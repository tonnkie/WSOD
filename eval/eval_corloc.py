import logging
import os
from datetime import datetime
import numpy as np
import tensorflow as tf
import cPickle as pickle

import data.bbox.image_processing as image_processing
import data.datasets as datasets
from network.rcnn import rcnn
import math
from utils.box_utils import get_iou_matrix
from utils.box_utils_tf import bbox_inv_transform, box_regression_transform, bbox_transform

tf.app.flags.DEFINE_string('dataset_name', 'Pascal', 'Name of dataset')
tf.app.flags.DEFINE_string('data_dir', None, 'Dataset directory')
tf.app.flags.DEFINE_string('image_set', 'trainval', 'Image set')
tf.app.flags.DEFINE_integer('image_size', 640, 'Input image size')
tf.app.flags.DEFINE_integer('batch_size', 1, 'Batch size.')
tf.app.flags.DEFINE_integer('num_proposals', 1000, 'Num of proposals')
tf.app.flags.DEFINE_string('checkpoint', None, 'Continue training from previous checkpoint')
tf.app.flags.DEFINE_bool('multilabel', False, 'Multi label dataset')
tf.app.flags.DEFINE_bool('save_results', False, 'Save results or not')
FLAGS = tf.app.flags.FLAGS


def main(unused):
  logdir = os.path.dirname(FLAGS.checkpoint)
  log_filename = os.path.join(logdir, 'corloc_eval_%s.log' % datetime.now())
  logging.basicConfig(filename=log_filename, level=logging.DEBUG)
  logging.info('Ckpt to be evaluated: %s' % FLAGS.checkpoint)

  thres = 0.5
  num_proposals = FLAGS.num_proposals

  g = tf.Graph()
  with g.as_default():
    with tf.device('/cpu:0'):
      # dataset
      dataset_to_call = getattr(datasets, FLAGS.dataset_name)
      dataset = dataset_to_call(subset=FLAGS.image_set,
                                datadir=FLAGS.data_dir)
      assert dataset.data_files()
      images, filenames, _, labels, rois, roi_labels, obj_labels, obj_bboxes = \
        image_processing.batch_inputs(dataset,
                                      image_size=FLAGS.image_size,
                                      train=False,
                                      max_num_proposals=10000,
                                      batch_size=FLAGS.batch_size)

      rois = rois[:, 0:num_proposals, :]
    logits, scores_and_bboxes_delta, _ = rcnn(images, rois, dataset.num_classes(), train=False)
    scores = scores_and_bboxes_delta[0]
    bboxes_delta = scores_and_bboxes_delta[1]
    bboxes_delta = tf.reshape(bboxes_delta, [-1, 4])
    roi_tile = tf.tile(rois, [1, 1, dataset.num_classes()])
    roi_tile_inv_trans = bbox_inv_transform(tf.reshape(roi_tile, [-1, 4]))
    bboxes = box_regression_transform(roi_tile_inv_trans, bboxes_delta)
    bboxes = bbox_transform(bboxes)
    bboxes = tf.reshape(bboxes, [FLAGS.batch_size, -1, dataset.num_classes()*4])
    bboxes = roi_tile
    scores_and_bboxes = []
    for cls in range(0, dataset.num_classes()):
      selected_box_indices = tf.image.non_max_suppression(bboxes[0, :, cls*4:cls*4+4], scores[0, :, cls],
                                                          2000, iou_threshold=0.8)
      selected_boxes = tf.gather(bboxes[0, :, cls*4:cls*4+4], selected_box_indices)
      selected_box_scores = tf.gather(scores[0, :, cls], selected_box_indices)
      selected_box_scores = tf.expand_dims(selected_box_scores, 1)
      scores_and_bboxes.append(tf.concat(1, [selected_box_scores, selected_boxes]))

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    sess = tf.Session(config=config)

    # Restore
    variables_to_restore = tf.trainable_variables()
    restorer = tf.train.Saver(variables_to_restore)
    restorer.restore(sess, FLAGS.checkpoint)

    num_iter = int(math.ceil(dataset.num_examples_per_epoch() / FLAGS.batch_size))

    proposal_budget = [1, 5, 10, 50, 100, num_proposals]
    all_gt = np.zeros(dataset.num_classes())
    all_tp = np.zeros((dataset.num_classes(), len(proposal_budget)))
    all_gt_obj = np.zeros(dataset.num_classes())
    all_tp_obj = np.zeros(dataset.num_classes())
    all_detections = {}
    step = 0
    tf.train.start_queue_runners(sess=sess)
    while step < 7306: #num_iter:
      print 'Evaluate batch %d/%d'%(step, num_iter)
      filenames_out, scores_and_bboxes_out, labels_out, gt_bboxes_out, gt_labels_out = sess.run(
        [filenames, scores_and_bboxes, roi_labels, obj_bboxes, obj_labels])
      if not (filenames_out[0] in all_detections):
        all_detections[filenames_out[0]] = {}
      labels_out = labels_out[0]
      all_gt += np.maximum(0, labels_out)
      gt_bboxes_out = gt_bboxes_out[0]
      gt_labels_out = gt_labels_out[0]
      for i in range(1, dataset.num_classes()):
        if labels_out[i] > 0:
          det = scores_and_bboxes_out[i][0:num_proposals, 1:]
          all_detections[filenames_out[0]][dataset.category_list()[i]] = scores_and_bboxes_out[i][0:num_proposals]
          gt = gt_bboxes_out[gt_labels_out == i, :]
          # get overlap matrix
          iou_matrix = get_iou_matrix(gt, det)
          all_gt_obj[i] += gt.shape[0]
          all_tp_obj[i] += np.sum(np.max(iou_matrix, axis=1) > thres)
          for k in range(len(proposal_budget)):
            all_tp[i, k] += (np.max(iou_matrix[:, 0:proposal_budget[k]]) > thres)
      step += 1
    if FLAGS.save_results:
      with open(os.path.join(logdir, 'Detections.pickle'), 'wb') as f:
        pickle.dump(all_detections, f)

    logging.info('----Corloc----')
    for i in range(1, dataset.num_classes()):
      logging.info('%s: %.3f' % (dataset.category_list()[i], all_tp[i, 0] / all_gt[i]))
    for i in range(len(proposal_budget)):
      logging.info('Proposal budget %d: %.3f' % (proposal_budget[i],
                                                 np.sum(all_tp[1:, i] / all_gt[1:]) / (dataset.num_classes()-1)))
      print 'Proposal budget %d: %.3f' % (proposal_budget[i],
                                                 np.sum(all_tp[1:, i] / all_gt[1:]) / (dataset.num_classes()-1))
    print 'Instance %d/%d'%(np.sum(all_tp[:, 0]), np.sum(all_gt))
    logging.info('Instance %d/%d'%(np.sum(all_tp[:, 0]), np.sum(all_gt)))
    logging.info('----Recall-0.5----')
    for i in range(1, 21):
      logging.info("Recall-0.5 for %s: %.3f" % (dataset.category_list()[i], all_tp_obj[i] / all_gt_obj[i]))

    logging.info('Avg Recall-0.5 with %d proposals: %.3f'
                 %(num_proposals,
                   np.sum(all_tp_obj[1:] / all_gt_obj[1:]) / (dataset.num_classes() - 1)))


if __name__ == '__main__':
  if not FLAGS.checkpoint:
    print('Must provide a checkpoint!')
  else:
    tf.app.run()