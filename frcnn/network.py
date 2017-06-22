import numpy as np
import sonnet as snt
import tensorflow as tf

from .dataset import TFRecordDataset
from .pretrained import VGG
from .rcnn import RCNN
from .roi_pool import ROIPoolingLayer
from .rpn import RPN

from .utils.generate_anchors import generate_anchors as generate_anchors_reference
from .utils.ops import meshgrid
from .utils.image import draw_bboxes


class FasterRCNN(snt.AbstractModule):
    """Faster RCNN Network"""

    def __init__(self, config, num_classes=None, name='fasterrcnn'):
        super(FasterRCNN, self).__init__(name=name)

        self._cfg = config
        self._num_classes = num_classes

        self._anchor_base_size = self._cfg.ANCHOR_BASE_SIZE
        self._anchor_scales = np.array(self._cfg.ANCHOR_SCALES)
        self._anchor_ratios = np.array(self._cfg.ANCHOR_RATIOS)
        self._anchor_stride = self._cfg.ANCHOR_STRIDE

        self._anchor_reference = generate_anchors_reference(
            self._anchor_base_size, self._anchor_ratios, self._anchor_scales
        )
        self._num_anchors = self._anchor_reference.shape[0]


        self._rpn_cls_loss_weight = 1.0
        self._rpn_reg_loss_weight = 2.0

        self._rcnn_cls_loss_weight = 1.0
        self._rcnn_reg_loss_weight = 2.0

        with self._enter_variable_scope():
            self._pretrained = VGG(trainable=self._cfg.PRETRAINED_TRAINABLE)
            self._rpn = RPN(self._num_anchors)
            self._roi_pool = ROIPoolingLayer()
            self._rcnn = RCNN(self._num_classes)

    def _build(self, image, gt_boxes, is_training=True):
        """
        Returns bounding boxes and classification probabilities.

        Args:
            image: A tensor with the image.
                Its shape should be `(1, height, width, 3)`.
            gt_boxes: A tensor with all the ground truth boxes of that image.
                Its shape should be `(num_gt_boxes, 4)`
                Where for each gt box we have (x1, y1, x2, y2), in that order.

        Returns:
            classification_prob: A tensor with the softmax probability for
                each of the bounding boxes found in the image.
                Its shape should be: (num_bboxes, num_categories + 1)
            classification_bbox: A tensor with the bounding boxes found.
                It's shape should be: (num_bboxes, 4). For each of the bboxes
                we have (x1, y1, x2, y2)
        """
        image_shape = tf.shape(image)[1:3]
        pretrained_output = self._pretrained(image)
        all_anchors = self._generate_anchors(pretrained_output)
        rpn_prediction = self._rpn(
            pretrained_output, gt_boxes, image_shape, all_anchors, is_training=is_training)

        roi_pool = self._roi_pool(rpn_prediction['proposals'], pretrained_output)

        # TODO: Missing mapping classification_bbox to real coordinates.
        # (and trimming, and NMS?)
        # TODO: Missing gt_boxes labels!
        classification_prediction = self._rcnn(roi_pool, rpn_prediction['proposals'], gt_boxes)

        # We need to apply bbox_transform_inv using classification_bbox delta and NMS?
        drawn_image = draw_bboxes(image, rpn_prediction['proposals'])

        tf.summary.image('image', image, max_outputs=20)
        tf.summary.image('top_10_rpn_boxes', drawn_image, max_outputs=20)
        # TODO: We should return a "prediction_dict" with all the required tensors (for results, loss and monitoring)
        return {
            'image_shape': image_shape,
            'all_anchors': all_anchors,
            'rpn_prediction': rpn_prediction,
            'classification_prediction': classification_prediction,
            'roi_pool': roi_pool,
            'gt_boxes': gt_boxes,
            'rpn_drawn_image': drawn_image,
        }

    def load_from_checkpoints(self, checkpoints):
        pass

    def loss(self, prediction_dict):
        """
        Compute the joint training loss for Faster RCNN.
        """
        rpn_loss_dict = self._rpn.loss(
            prediction_dict['rpn_prediction']
        )

        rcnn_loss_dict = self._rcnn.loss(
            prediction_dict['classification_prediction']
        )

        # Losses have a weight assigned.
        rpn_loss_dict['rpn_cls_loss'] = rpn_loss_dict['rpn_cls_loss'] * self._rpn_cls_loss_weight
        rpn_loss_dict['rpn_reg_loss'] = rpn_loss_dict['rpn_reg_loss'] * self._rpn_reg_loss_weight

        rcnn_loss_dict['rcnn_cls_loss'] = rcnn_loss_dict['rcnn_cls_loss'] * self._rcnn_cls_loss_weight
        rcnn_loss_dict['rcnn_reg_loss'] = rcnn_loss_dict['rcnn_reg_loss'] * self._rcnn_reg_loss_weight

        for loss_name, loss_tensor in list(rpn_loss_dict.items()) + list(rcnn_loss_dict.items()):
            tf.summary.scalar(loss_name, loss_tensor, collections=['Losses'])
            tf.losses.add_loss(loss_tensor)

        total_loss = tf.losses.get_total_loss()
        tf.summary.scalar('total_loss', total_loss, collections=['Losses'])
        return total_loss

    def _generate_anchors(self, feature_map):
        feature_map_shape = tf.shape(feature_map)[1:3]
        grid_width = feature_map_shape[1]
        grid_height = feature_map_shape[0]
        shift_x = tf.range(grid_width) * self._anchor_stride
        shift_y = tf.range(grid_height) * self._anchor_stride
        shift_x, shift_y = meshgrid(shift_x, shift_y)

        shift_x = tf.reshape(shift_x, [-1])
        shift_y = tf.reshape(shift_y, [-1])

        shifts = tf.stack(
            [shift_x, shift_y, shift_x, shift_y],
            axis=0
        )

        shifts = tf.transpose(shifts)
        # Shifts now is a (H x W, 4) Tensor

        # TODO: We should implement anchor_reference as Tensor
        num_anchors = self._anchor_reference.shape[0]
        num_anchor_points = tf.shape(shifts)[0]

        all_anchors = (
            self._anchor_reference.reshape((1, num_anchors, 4)) +
            tf.transpose(tf.reshape(shifts, (1, num_anchor_points, 4)), (1, 0, 2))
        )

        all_anchors = tf.reshape(all_anchors, (num_anchors * num_anchor_points, 4))

        return all_anchors
