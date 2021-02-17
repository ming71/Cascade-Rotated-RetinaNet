import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from utils.bbox import bbox_overlaps, min_area_square
from utils.box_coder import BoxCoder
from utils.overlaps.rbox_overlaps import rbox_overlaps


def xyxy2xywh_a(query_boxes):
    out_boxes = query_boxes.copy()
    out_boxes[:, 0] = (query_boxes[:, 0] + query_boxes[:, 2]) * 0.5
    out_boxes[:, 1] = (query_boxes[:, 1] + query_boxes[:, 3]) * 0.5
    out_boxes[:, 2] = query_boxes[:, 2] - query_boxes[:, 0]
    out_boxes[:, 3] = query_boxes[:, 3] - query_boxes[:, 1]
    return out_boxes

# cuda_overlaps
class IntegratedLoss(nn.Module):
    def __init__(self, alpha=0.25, gamma=2.0, func = 'smooth'):
        super(IntegratedLoss, self).__init__()
        self.alpha = alpha
        self.gamma = gamma
        self.box_coder = BoxCoder()
        if func == 'smooth':
            self.criteron = smooth_l1_loss
        elif func == 'mse':
            self.criteron = F.mse_loss
        elif func == 'balanced':
            self.criteron = balanced_l1_loss
            
    def forward(self, classifications, regressions, anchors, annotations,iou_thres=0.5):
        
        cls_losses = []
        reg_losses = []
        batch_size = classifications.shape[0]
        all_pred_boxes = self.box_coder.decode(anchors, regressions, mode='xywht')
        for j in range(batch_size):
            classification = classifications[j, :, :]
            regression = regressions[j, :, :]
            bbox_annotation = annotations[j, :, :]
            bbox_annotation = bbox_annotation[bbox_annotation[:, -1] != -1]
            pred_boxes = all_pred_boxes[j, :, :]
            if bbox_annotation.shape[0] == 0:
                cls_losses.append(torch.tensor(0).float().cuda())
                reg_losses.append(torch.tensor(0).float().cuda())
                continue
            classification = torch.clamp(classification, 1e-4, 1.0 - 1e-4)
            indicator = bbox_overlaps(
                min_area_square(anchors[j, :, :]),
                min_area_square(bbox_annotation[:, :-1])
            )
            ious = rbox_overlaps(
                anchors[j, :, :].cpu().numpy(),
                bbox_annotation[:, :-1].cpu().numpy(),
                indicator.cpu().numpy(),
                thresh=1e-1
            )
            if not torch.is_tensor(ious):
                ious = torch.from_numpy(ious).cuda()
            
            iou_max, iou_argmax = torch.max(ious, dim=1)
           
            positive_indices = torch.ge(iou_max, iou_thres)

            max_gt, argmax_gt = ious.max(0) 
            if (max_gt < iou_thres).any():
                positive_indices[argmax_gt[max_gt < iou_thres]]=1
              
            # cls loss
            cls_targets = (torch.ones(classification.shape) * -1).cuda()
            cls_targets[torch.lt(iou_max, iou_thres - 0.1), :] = 0
            num_positive_anchors = positive_indices.sum()
            assigned_annotations = bbox_annotation[iou_argmax, :]
            cls_targets[positive_indices, :] = 0
            cls_targets[positive_indices, assigned_annotations[positive_indices, -1].long()] = 1
            alpha_factor = torch.ones(cls_targets.shape).cuda() * self.alpha
            alpha_factor = torch.where(torch.eq(cls_targets, 1.), alpha_factor, 1. - alpha_factor)
            focal_weight = torch.where(torch.eq(cls_targets, 1.), 1. - classification, classification)
            focal_weight = alpha_factor * torch.pow(focal_weight, self.gamma)
            bin_cross_entropy = -(cls_targets * torch.log(classification+1e-6) + (1.0 - cls_targets) * torch.log(1.0 - classification+1e-6))
            cls_loss = focal_weight * bin_cross_entropy 
            cls_loss = torch.where(torch.ne(cls_targets, -1.0), cls_loss, torch.zeros(cls_loss.shape).cuda())
            cls_losses.append(cls_loss.sum() / torch.clamp(num_positive_anchors.float(), min=1.0))
            # reg loss
            if positive_indices.sum() > 0:
                all_rois = anchors[j, positive_indices, :]
                gt_boxes = assigned_annotations[positive_indices, :]
                reg_targets = self.box_coder.encode(all_rois, gt_boxes)
                reg_loss = self.criteron(regression[positive_indices, :], reg_targets)
                reg_losses.append(reg_loss)

                if not torch.isfinite(reg_loss) :
                    import ipdb; ipdb.set_trace()
            else:
                reg_losses.append(torch.tensor(0).float().cuda())
        loss_cls = torch.stack(cls_losses).mean(dim=0, keepdim=True)
        loss_reg = torch.stack(reg_losses).mean(dim=0, keepdim=True)
        return loss_cls, loss_reg

    


class RegressLoss(nn.Module):
    def __init__(self, func='smooth'):
        super(RegressLoss, self).__init__()
        self.box_coder = BoxCoder()
        if func == 'smooth':
            self.criteron = smooth_l1_loss
        elif func == 'mse':
            self.criteron = F.mse_loss
        elif func == 'balanced':
            self.criteron = balanced_l1_loss
        else:
            raise NotImplementedError
            
    def forward(self, regressions, anchors, annotations, iou_thres=0.5):
        losses = []
        batch_size = regressions.shape[0]
        all_pred_boxes = self.box_coder.decode(anchors, regressions, mode='xywht')
        for j in range(batch_size):
            regression = regressions[j, :, :]
            bbox_annotation = annotations[j, :, :]
            bbox_annotation = bbox_annotation[bbox_annotation[:, -1] != -1]
            pred_boxes = all_pred_boxes[j, :, :]
            if bbox_annotation.shape[0] == 0:
                losses.append(torch.tensor(0).float().cuda())
                continue
            indicator = bbox_overlaps(
                min_area_square(anchors[j, :, :]),
                min_area_square(bbox_annotation[:, :-1])
            )
            overlaps = rbox_overlaps(
                anchors[j, :, :].cpu().numpy(),
                bbox_annotation[:, :-1].cpu().numpy(),
                indicator.cpu().numpy(),
                thresh=1e-1
            )
            if not torch.is_tensor(overlaps):
                overlaps = torch.from_numpy(overlaps).cuda()

            iou_max, iou_argmax = torch.max(overlaps, dim=1)
            positive_indices = torch.ge(iou_max, iou_thres)
            # MaxIoU assigner
            max_gt, argmax_gt = overlaps.max(0) 
            if (max_gt < iou_thres).any():
                positive_indices[argmax_gt[max_gt < iou_thres]]=1

            assigned_annotations = bbox_annotation[iou_argmax, :]
            if positive_indices.sum() > 0:
                all_rois = anchors[j, positive_indices, :]
                gt_boxes = assigned_annotations[positive_indices, :]
                targets = self.box_coder.encode(all_rois, gt_boxes)
                loss = self.criteron(regression[positive_indices, :], targets)
                losses.append(loss)
            else:
                losses.append(torch.tensor(0).float().cuda())
        return torch.stack(losses).mean(dim=0, keepdim=True)




def smooth_l1_loss(inputs,
                   targets,
                   beta=1. / 9,
                   size_average=True,
                   weight = None):
    """
    https://github.com/facebookresearch/maskrcnn-benchmark
    """
    diff = torch.abs(inputs - targets)
    if  weight is  None:
        loss = torch.where(
            diff < beta,
            0.5 * diff ** 2 / beta,
            diff - 0.5 * beta
        )
    else:
        loss = torch.where(
            diff < beta,
            0.5 * diff ** 2 / beta,
            diff - 0.5 * beta
        ) * weight.unsqueeze(1)
    if size_average:
        return loss.mean()
    return loss.sum()


def balanced_l1_loss(inputs,
                     targets,
                     beta=1. / 9,
                     alpha=0.5,
                     gamma=1.5,
                     size_average=True):
    """Balanced L1 Loss

    arXiv: https://arxiv.org/pdf/1904.02701.pdf (CVPR 2019)
    """
    assert beta > 0
    assert inputs.size() == targets.size() and targets.numel() > 0

    diff = torch.abs(inputs - targets)
    b = np.e**(gamma / alpha) - 1
    loss = torch.where(
        diff < beta, alpha / b *
        (b * diff + 1) * torch.log(b * diff / beta + 1) - alpha * diff,
        gamma * diff + gamma / b - alpha * beta)

    if size_average:
        return loss.mean()
    return loss.sum()

