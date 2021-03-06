from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from paddle import fluid
from paddle.fluid.param_attr import ParamAttr
from paddle.fluid.regularizer import L2Decay


def DropBlock(input, block_size, keep_prob, is_test):
    if is_test:
        return input

    def CalculateGamma(input, block_size, keep_prob):
        input_shape = fluid.layers.shape(input)
        feat_shape_tmp = fluid.layers.slice(input_shape, [0], [3], [4])
        feat_shape_tmp = fluid.layers.cast(feat_shape_tmp, dtype="float32")
        feat_shape_t = fluid.layers.reshape(feat_shape_tmp, [1, 1, 1, 1])
        feat_area = fluid.layers.pow(feat_shape_t, factor=2)

        block_shape_t = fluid.layers.fill_constant(
            shape=[1, 1, 1, 1], value=block_size, dtype='float32')
        block_area = fluid.layers.pow(block_shape_t, factor=2)

        useful_shape_t = feat_shape_t - block_shape_t + 1
        useful_area = fluid.layers.pow(useful_shape_t, factor=2)

        upper_t = feat_area * (1 - keep_prob)
        bottom_t = block_area * useful_area
        output = upper_t / bottom_t
        return output

    gamma = CalculateGamma(input, block_size=block_size, keep_prob=keep_prob)
    input_shape = fluid.layers.shape(input)
    p = fluid.layers.expand_as(gamma, input)

    input_shape_tmp = fluid.layers.cast(input_shape, dtype="int64")
    random_matrix = fluid.layers.uniform_random(
        input_shape_tmp, dtype='float32', min=0.0, max=1.0)
    one_zero_m = fluid.layers.less_than(random_matrix, p)
    one_zero_m.stop_gradient = True
    one_zero_m = fluid.layers.cast(one_zero_m, dtype="float32")

    mask_flag = fluid.layers.pool2d(
        one_zero_m,
        pool_size=block_size,
        pool_type='max',
        pool_stride=1,
        pool_padding=block_size // 2)
    mask = 1.0 - mask_flag

    elem_numel = fluid.layers.reduce_prod(input_shape)
    elem_numel_m = fluid.layers.cast(elem_numel, dtype="float32")
    elem_numel_m.stop_gradient = True

    elem_sum = fluid.layers.reduce_sum(mask)
    elem_sum_m = fluid.layers.cast(elem_sum, dtype="float32")
    elem_sum_m.stop_gradient = True

    output = input * mask * elem_numel_m / elem_sum_m
    return output


class YOLOv3Head(object):

    def __init__(self,
                 norm_decay=0.,
                 num_classes=80,
                 ignore_thresh=0.7,
                 label_smooth=True,
                 nms_keep_topk=100,
                 nms_threshold=0.45,
                 score_threshold=0.01,
                 anchors=[[10, 13], [16, 30], [33, 23], [30, 61], [62, 45],
                          [59, 119], [116, 90], [156, 198], [373, 326]],
                 anchor_masks=[[6, 7, 8], [3, 4, 5], [0, 1, 2]],
                 freeze_block=[],
                 freeze_route=[],
                 freeze_norm=False,
                 drop_block=False,
                 block_size=3,
                 keep_prob=0.9,
                 weight_prefix_name=''):
        self.norm_decay = norm_decay
        self.freeze_block = freeze_block
        self.freeze_route = freeze_route
        self.freeze_norm = freeze_norm
        self.drop_block = drop_block
        self.block_size = block_size
        self.keep_prob = keep_prob
        self.num_classes = num_classes
        self.nms_keep_topk = nms_keep_topk
        self.nms_threshold = nms_threshold
        self.score_threshold = score_threshold
        self.ignore_thresh = ignore_thresh
        self.label_smooth = label_smooth
        self.anchor_masks = anchor_masks
        self._parse_anchors(anchors)
        self.prefix_name = weight_prefix_name

    def _conv_bn(self,
                 input,
                 ch_out,
                 filter_size,
                 stride,
                 padding,
                 act='leaky',
                 is_test=True,
                 name=None):
        conv = fluid.layers.conv2d(
            input=input,
            num_filters=ch_out,
            filter_size=filter_size,
            stride=stride,
            padding=padding,
            act=None,
            param_attr=ParamAttr(name=name + ".conv.weights"),
            bias_attr=False)

        norm_lr = 0. if self.freeze_norm else 1.
        bn_name = name + ".bn"
        bn_param_attr = ParamAttr(
            regularizer=L2Decay(self.norm_decay),
            learning_rate=norm_lr,
            name=bn_name + '.scale')
        bn_bias_attr = ParamAttr(
            regularizer=L2Decay(self.norm_decay),
            learning_rate=norm_lr,
            name=bn_name + '.offset')
        out = fluid.layers.batch_norm(
            input=conv,
            act=None,
            is_test=is_test,
            param_attr=bn_param_attr,
            bias_attr=bn_bias_attr,
            moving_mean_name=bn_name + '.mean',
            moving_variance_name=bn_name + '.var')

        if act == 'leaky':
            out = fluid.layers.leaky_relu(x=out, alpha=0.1)
        return out

    def _detection_block(self, input, channel, is_test=True, name=None):
        assert channel % 2 == 0, \
            "channel {} cannot be divided by 2 in detection block {}" \
            .format(channel, name)

        conv = input
        for j in range(2):
            conv = self._conv_bn(
                conv,
                channel,
                filter_size=1,
                stride=1,
                padding=0,
                is_test=is_test,
                name='{}.{}.0'.format(name, j))
            conv = self._conv_bn(
                conv,
                channel * 2,
                filter_size=3,
                stride=1,
                padding=1,
                is_test=is_test,
                name='{}.{}.1'.format(name, j))

            if self.drop_block and j == 0 and channel != 512:
                conv = DropBlock(
                    conv,
                    block_size=self.block_size,
                    keep_prob=self.keep_prob,
                    is_test=is_test)

        if self.drop_block and channel == 512:
            conv = DropBlock(
                conv,
                block_size=self.block_size,
                keep_prob=self.keep_prob,
                is_test=is_test)

        route = self._conv_bn(
            conv,
            channel,
            filter_size=1,
            stride=1,
            padding=0,
            is_test=is_test,
            name='{}.2'.format(name))
        tip = self._conv_bn(
            route,
            channel * 2,
            filter_size=3,
            stride=1,
            padding=1,
            is_test=is_test,
            name='{}.tip'.format(name))
        return route, tip

    def _upsample(self, input, scale=2, name=None):
        out = fluid.layers.resize_nearest(
            input=input, scale=float(scale), name=name)
        return out

    def _parse_anchors(self, anchors):
        """
        Check ANCHORS/ANCHOR_MASKS in config and parse mask_anchors

        """
        self.anchors = []
        self.mask_anchors = []

        assert len(anchors) > 0, "ANCHORS not set."
        assert len(self.anchor_masks) > 0, "ANCHOR_MASKS not set."

        for anchor in anchors:
            assert len(anchor) == 2, "anchor {} len should be 2".format(anchor)
            self.anchors.extend(anchor)

        anchor_num = len(anchors)
        for masks in self.anchor_masks:
            self.mask_anchors.append([])
            for mask in masks:
                assert mask < anchor_num, "anchor mask index overflow"
                self.mask_anchors[-1].extend(anchors[mask])

    def _get_outputs(self, input, is_train=True):
        """
        Get YOLOv3 head output

        Args:
            input (list): List of Variables, output of backbone stages
            is_train (bool): whether in train or test mode

        Returns:
            outputs (list): Variables of each output layer
        """

        outputs = []

        # get last out_layer_num blocks in reverse order
        out_layer_num = len(self.anchor_masks)
        blocks = input[-1:-out_layer_num - 1:-1]

        route = None
        for i, block in enumerate(blocks):
            if i > 0:  # perform concat in first 2 detection_block
                block = fluid.layers.concat(input=[route, block], axis=1)
            route, tip = self._detection_block(
                block,
                channel=512 // (2**i),
                is_test=(not is_train),
                name=self.prefix_name + "yolo_block.{}".format(i))

            if i in self.freeze_route:
                route.stop_gradient = True

            if i in self.freeze_block:
                tip.stop_gradient = True

            # out channel number = mask_num * (5 + class_num)
            num_filters = len(self.anchor_masks[i]) * (self.num_classes + 5)
            block_out = fluid.layers.conv2d(
                input=tip,
                num_filters=num_filters,
                filter_size=1,
                stride=1,
                padding=0,
                act=None,
                param_attr=ParamAttr(name=self.prefix_name + "yolo_output.{}.conv.weights".format(i)),
                bias_attr=ParamAttr(
                    regularizer=L2Decay(0.),
                    name=self.prefix_name + "yolo_output.{}.conv.bias".format(i)))
            outputs.append(block_out)

            if i < len(blocks) - 1:
                route = self._conv_bn(
                    input=route,
                    ch_out=256 // (2**i),
                    filter_size=1,
                    stride=1,
                    padding=0,
                    is_test=(not is_train),
                    name=self.prefix_name + "yolo_transition.{}".format(i))
                # upsample
                route = self._upsample(route)

        return outputs

    def get_loss(self, outputs, gt_box, gt_label, gt_score):
        """
        Get final loss of network of YOLOv3.

        Args:
            outputs (list): List of Variables, output of backbone stages
            gt_box (Variable): The ground-truth boudding boxes.
            gt_label (Variable): The ground-truth class labels.
            gt_score (Variable): The ground-truth boudding boxes mixup scores.

        Returns:
            loss (Variable): The loss Variable of YOLOv3 network.

        """
        losses = []
        downsample = 32
        for i, output in enumerate(outputs):
            anchor_mask = self.anchor_masks[i]
            loss = fluid.layers.yolov3_loss(
                x=output,
                gt_box=gt_box,
                gt_label=gt_label,
                gt_score=gt_score,
                anchors=self.anchors,
                anchor_mask=anchor_mask,
                class_num=self.num_classes,
                ignore_thresh=self.ignore_thresh,
                downsample_ratio=downsample,
                use_label_smooth=self.label_smooth,
                name=self.prefix_name + "yolo_loss" + str(i))
            losses.append(fluid.layers.reduce_mean(loss))
            downsample //= 2

        return sum(losses)

    def get_prediction(self, outputs, im_size, score_threshold=0.01):
        """
        Get prediction result of YOLOv3 network

        Args:
            outputs (list): List of Variables, output of backbone stages
            im_size (Variable): Variable of size([h, w]) of each image

        Returns:
            pred (Variable): The prediction result after non-max suppress.

        """
        boxes = []
        scores = []
        downsample = 32
        for i, output in enumerate(outputs):
            box, score = fluid.layers.yolo_box(
                x=output,
                img_size=im_size,
                anchors=self.mask_anchors[i],
                class_num=self.num_classes,
                conf_thresh=score_threshold,
                downsample_ratio=downsample,
                name=self.prefix_name + "yolo_box" + str(i))
            boxes.append(box)
            scores.append(fluid.layers.transpose(score, perm=[0, 2, 1]))

            downsample //= 2

        yolo_boxes = fluid.layers.concat(boxes, axis=1)
        yolo_scores = fluid.layers.concat(scores, axis=2)
        pred = fluid.layers.multiclass_nms(bboxes=yolo_boxes, scores=yolo_scores,
                                           score_threshold=self.score_threshold, nms_top_k=1000,
                                           keep_top_k=self.nms_keep_topk,
                                           nms_threshold=self.nms_threshold,
                                           normalized=False, nms_eta=1.0,
                                           background_label=-1)
        return {'bbox': pred}
