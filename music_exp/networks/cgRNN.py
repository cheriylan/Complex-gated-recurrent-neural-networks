# Do the imports.
import sys
import time
import pickle
import numpy as np
import tensorflow as tf
from custom_conv import complex_conv1D
# from scipy.fftpack import fft
from sklearn.metrics import average_precision_score
from IPython.core.debugger import Tracer
from music_net_handler import MusicNet
sys.path.insert(0, "../")
import custom_cells as cc

# import custom_optimizers as co
debug_here = Tracer()


# where to store the logfiles.
subfolder = 'cCNN_cgRNN'

m = 128         # number of notes
sampling_rate = 11000      # samples/second
features_idx = 0    # first element of (X,Y) data tuple
labels_idx = 1      # second element of (X,Y) data tuple

# Network parameters:
c = 24              # number of context vectors
batch_size = 5      # The number of data points to be processed in parallel.
d = [4, 8, 16]            # CNN filter depth.
filter_width = [3, 3, 3]  # cnn filter length
stride = [2, 2, 2]
assert len(d) == len(filter_width)
assert len(filter_width) == len(stride)

cell_size = 1296       # cell depth.
CNN = True
RNN = True
stiefel = False
dropout = True

# FFT parameters:
# window_size = 16384
# window_size = 4096
window_size = 2048

# Training parameters:
learning_rate = 0.0002
learning_rate_decay = 0.9
decay_iterations = 50000
iterations = 350000
GPU = [4]


def compute_parameter_total(trainable_variables):
    total_parameters = 0
    for variable in trainable_variables:
        # shape is an array of tf.Dimension
        shape = variable.get_shape()
        # print('var_name', variable.name, 'shape', shape, 'dim', len(shape))
        variable_parameters = 1
        for dim in shape:
            # print(dim)
            variable_parameters *= dim.value
        # print('parameters', variable_parameters)
        total_parameters += variable_parameters
    print('total:', total_parameters)
    return total_parameters


print('Setting up the tensorflow graph.')
train_graph = tf.Graph()
with train_graph.as_default():
    global_step = tf.Variable(0, trainable=False, name='global_step')
    # We use c input windows to give the RNN acces to context.
    x = tf.placeholder(tf.float32, shape=[batch_size, c, window_size])
    # The ground labelling is used during traning, wich random sampling
    # from the network output.
    y_gt = tf.placeholder(tf.float32, shape=[batch_size, c, m])

    # compute the fft in the time domain data.
    # xf = tf.spectral.fft(tf.complex(x, tf.zeros_like(x)))
    xf = tf.spectral.rfft(x)

    dec_learning_rate = tf.train.exponential_decay(learning_rate, global_step,
                                                   decay_iterations, learning_rate_decay,
                                                   staircase=True)
    optimizer = tf.train.RMSPropOptimizer(dec_learning_rate)
    tf.summary.scalar('learning_rate', dec_learning_rate)

    if CNN:
        with tf.variable_scope('complex_CNN'):
            xfd = tf.reshape(xf, [batch_size*c, -1])
            xfd = tf.expand_dims(xfd, -1)

            conv = [xfd]
            for layer_no, layer_d in enumerate(d):
                conv_tmp = complex_conv1D(conv[-1], filter_width=filter_width[layer_no],
                                          depth=layer_d, stride=stride[layer_no],
                                          padding='VALID', scope='_layer' + str(layer_no))
                conv.append(cc.split_relu(conv_tmp))
                print('conv2 shape', conv[-1].shape)
            flat = tf.reshape(conv[-1], [batch_size, c, -1])
            RNN_in = flat
    else:
        RNN_in = xf
    if RNN:
        def define_bidirecitonal(RNN_in, cell_size, stiefel, dropout, reuse=None):
            cell = cc.StiefelGatedRecurrentUnit(num_units=cell_size, stiefel=stiefel,
                                                num_proj=None, complex_input=True,
                                                dropout=dropout, reuse=reuse)
            # Bidirectional RNN encoder.
            outputs, states = tf.nn.bidirectional_dynamic_rnn(
                cell, cell, RNN_in, dtype=tf.float32)
            to_decode = tf.concat([tf.complex(outputs[0][:, :, :cell_size],
                                              outputs[0][:, :, cell_size:]),
                                   tf.complex(outputs[1][:, :, :cell_size],
                                              outputs[1][:, :, cell_size:])],
                                  axis=-1)
            # RNN decoder.
            decoder_cell = cc.StiefelGatedRecurrentUnit(
                num_units=int(cell_size), stiefel=stiefel, num_proj=m,
                complex_input=True, reuse=reuse)
            y, _ = tf.nn.dynamic_rnn(decoder_cell, to_decode,
                                     dtype=tf.float32)
            return y

        y = define_bidirecitonal(RNN_in, cell_size, stiefel, dropout)
        if dropout:
            print('test part of graph.')
            y_test = define_bidirecitonal(RNN_in, cell_size, stiefel,
                                          dropout=False, reuse=True)
        else:
            y_test = y
    else:
        if c != 1:
            raise ValueError("c must be one for non RNN networks.")
        y = tf.nn.sigmoid(cc.C_to_R(flat, m, reuse=None))
        y = y[:, -1, :]
        y_gt = y_gt[:, -1, :]

    # L = tf.losses.sigmoid_cross_entropy(y[:, -1, :], y_[:, -1, :])
    # L = tf.reduce_mean(tf.nn.l2_loss(y[:, -1, :] - y_[:, -1, :]))
    L = tf.losses.mean_squared_error(y_gt, y)
    L_test = tf.losses.mean_squared_error(y_gt, y_test)
    gvs = optimizer.compute_gradients(L)
    tf.summary.scalar('train_mse', L)
    # print(gvs)
    with tf.variable_scope("gradient_clipping"):
        capped_gvs = [(tf.clip_by_value(grad, -1., 1.), var) for grad, var in gvs]
        # capped_gvs = [(tf.clip_by_norm(grad, 2.0), var) for grad, var in gvs]
        # loss = tf.Print(loss, [tf.reduce_mean(gvs[0]) for gv in gvs])
        training_step = optimizer.apply_gradients(capped_gvs,
                                                  global_step=global_step)
    # training_step = optimizer.minimize(L)
    init_op = tf.global_variables_initializer()
    summary_op = tf.summary.merge_all()
    saver = tf.train.Saver()
    test_summary = tf.summary.scalar('test_mse', L_test)
    parameter_total = compute_parameter_total(tf.trainable_variables())

# Load the data.
print('Loading music-Net...')
musicNet = MusicNet(c, window_size, window_size, sampling_rate=sampling_rate)
batched_time_music_lst, batcheded_time_labels_lst = musicNet.get_test_batches(batch_size)

print('parameters:', m, sampling_rate, features_idx, labels_idx, c, batch_size,
      filter_width, d, window_size, stride,
      learning_rate, learning_rate_decay, iterations, GPU, CNN,
      dropout, parameter_total)

time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
param_str = 'lr_' + str(learning_rate) + '_lrd_' + str(learning_rate_decay) \
            + '_lrdi_' + str(decay_iterations) \
            + '_bs_' + str(batch_size) + '_ws_' + str(window_size) \
            + '_fs_' + str(sampling_rate)
if CNN:
    param_str += '_fw1_' + str(filter_width)  \
        + '_str_' + str(stride) + '_depth_' + str(d)
param_str += '_loss_' + str(L.name[:-8]) \
             + '_cnn_' + str(CNN) + '_dropout_' + str(dropout) \
             + '_cs_' + str(cell_size) + '_c_' + str(c) \
             + '_totparam_' + str(parameter_total)
savedir = './logs' + '/' + subfolder + '/' + time_str \
          + '_' + param_str
summary_writer = tf.summary.FileWriter(savedir, graph=train_graph)

square_error = []
average_precision = []
gpu_options = tf.GPUOptions(visible_device_list=str(GPU)[1:-1])
config = tf.ConfigProto(allow_soft_placement=True,
                        log_device_placement=False,
                        gpu_options=gpu_options)
with tf.Session(graph=train_graph, config=config) as sess:
    start = time.time()
    print('Initialize...')
    init_op.run(session=sess)

    print('Training...')
    for i in range(iterations):
        if i % 100 == 0 and (i != 0 or len(square_error) == 0):
            batch_time_music_test, batched_time_labels_test = \
                musicNet.get_batch(musicNet.test_data, musicNet.test_ids,
                                   batch_size)
            feed_dict = {x: batch_time_music_test,
                         y_gt: batched_time_labels_test}
            L_np, test_summary_eval, global_step_eval = sess.run([L_test, test_summary,
                                                                 global_step],
                                                                 feed_dict=feed_dict)
            square_error.append(L_np)
            summary_writer.add_summary(test_summary_eval, global_step=global_step_eval)

        if i % 5000 == 0:
            # run trough the entire test set.
            yflat = np.array([])
            yhatflat = np.array([])
            losses_lst = []
            for j in range(len(batched_time_music_lst)):
                batch_time_music = batched_time_music_lst[j]
                batched_time_labels = batcheded_time_labels_lst[j]
                feed_dict = {x: batch_time_music,
                             y_gt: batched_time_labels}
                loss, Yhattest, np_global_step =  \
                    sess.run([L_test, y_test, global_step], feed_dict=feed_dict)
                losses_lst.append(loss)
                center = int(c/2.0)
                yhatflat = np.append(yhatflat, Yhattest[:, center, :].flatten())
                yflat = np.append(yflat, batched_time_labels[:, center, :].flatten())
            average_precision.append(average_precision_score(yflat,
                                                             yhatflat))
            end = time.time()
            print(i, '\t', round(np.mean(losses_lst), 8),
                     '\t', round(average_precision[-1], 8),
                     '\t', round(end-start, 8))
            saver.save(sess, savedir + '/weights', global_step=np_global_step)
            start = time.time()

        batch_time_music, batched_time_labels = \
            musicNet.get_batch(musicNet.train_data, musicNet.train_ids, batch_size)
        feed_dict = {x: batch_time_music,
                     y_gt: batched_time_labels}
        loss, out_net, out_gt, _, summaries, np_global_step = \
            sess.run([L, y, y_gt, training_step, summary_op, global_step],
                     feed_dict=feed_dict)
        summary_writer.add_summary(summaries, global_step=np_global_step)

    # save the network
    saver.save(sess, savedir + '/weights/', global_step=np_global_step)
    pickle.dump(average_precision, open(savedir + "/avgprec.pkl", "wb"))