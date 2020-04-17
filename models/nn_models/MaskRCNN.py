import os
import time
import sys

import yaml
import numpy as np
import cv2
import pandas as pd
from scipy.spatial import distance
sys.path.append('../models/*')
from mrcnn.config import Config
from mrcnn import model as modellib
from collections import defaultdict
from models.utils.smooth import smooth_points


class MRCNNLogoInsertion():

    def __init__(self):
        self.model = None
        self.frame = None
        self.masks = None
        self.frame_num = 0
        self.load_smooth = True
        self.detection_successful = False
        self.corners = None
        self.replace = None
        self.center_left = None
        self.center_right = None
        self.fps = None
        self.key = None
        self.start = None
        self.finish = None
        self.config = None
        self.process = False
        self.to_replace = None
        self.point_ids = list()
        self.class_match = defaultdict(list)
        self.before_smoothing = True
        self.mask_id = None
        self.class_ids = list()
        self.masks = list()
        self.banner_id = None
        self.masks_path = None
        self.saved_points = pd.DataFrame(columns=['x_top_left', 'y_top_left', 'x_top_right', 'y_top_right',
                                                  'x_bot_left', 'y_bot_left', 'x_bot_right', 'y_bot_right'])

    def init_params(self, params):

        with open(params) as f:
            self.config = yaml.load(f, Loader=yaml.FullLoader)

        self.replace = self.config['replace']
        self.to_replace = list(self.replace.keys())
        self.masks_path = self.config['masks_path']

        if not os.path.exists(self.masks_path):
            os.mkdir(self.masks_path)

        if bool(self.config['periods']):
            self.key = list(self.config['periods'].keys())[0]
            self.start, self.finish = self.config['periods'][self.key].values()
        else:
            self.process = True

    def __valid_time(self):
        if self.key:
            times = self.frame_num / self.fps
            if (self.start <= times) and (times <= self.finish):
                self.process = True
            else:
                self.process = False

            if times == self.finish:
                print(f"Ended {self.key.split('_')[0]} {self.key.split('_')[1]}")
                del self.config['periods'][self.key]
                if len(self.config['periods'].keys()):
                    self.key = list(self.config['periods'].keys())[0]
                    self.start, self.finish = self.config['periods'][self.key].values()

    def detect_banner(self, frame):

        self.frame = frame
        self.__valid_time()
        if self.process:
            if self.before_smoothing:
                self.__detect_mask()
                for mask_id, class_id in enumerate(self.class_ids):
                    mask = self.masks[mask_id]
                    self.__check_contours(mask, class_id, mask_id)
            else:
                if self.frame_num in self.class_match:
                    self.detection_successful = True
                else:
                    self.detection_successful = False
        self.frame_num += 1

    def __detect_mask(self):
        rgb_frame = np.flip(self.frame, 2)  # convert color from bgr to rgb
        result = self.model.detect([rgb_frame])[0]
        class_ids = result['class_ids']
        masks = result['masks']
        self.masks.clear()
        self.class_ids.clear()
        for i, class_id in enumerate(class_ids):
            if class_id in self.to_replace:
                mask = masks[:, :, i].astype(np.float32)
                self.masks.append(mask)
                self.class_ids.append(class_id)

    def __check_contours(self, fsz_mask, class_id, mask_id):

        # load parameters
        filter_area_size = self.config['filter_area_size']

        # finding contours
        first_cnt = True
        _, thresh = cv2.threshold(fsz_mask, 0.5, 255, 0)
        thresh = thresh.astype(np.uint8)
        _, contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            if cv2.contourArea(cnt) > filter_area_size:
                rect = cv2.minAreaRect(cnt)
                box = cv2.boxPoints(rect).astype(np.float16)
                xm, ym = rect[0]
                if first_cnt:
                    first_cnt = False
                    left_ids = np.argwhere(box[:, 0] < xm).squeeze()
                    left = box[left_ids]
                    right = np.delete(box, np.s_[left_ids], 0)
                    top_left, bot_left = left[left[:, 1].argsort(axis=0)]
                    top_right, bot_right = right[right[:, 1].argsort(axis=0)]

                    self.center_left = xm
                    self.center_right = xm
                else:
                    left_ids = np.argwhere(box[:, 0] < xm).squeeze()
                    if xm < self.center_left:
                        left = box[left_ids]
                        top_left, bot_left = left[left[:, 1].argsort(axis=0)]
                        self.center_left = xm
                    elif xm > self.center_right:
                        right = np.delete(box, np.s_[left_ids], 0)
                        top_right, bot_right = right[right[:, 1].argsort(axis=0)]
                        self.center_right = xm

                cv2.drawContours(fsz_mask, [cnt], -1, (1), -1)
        if first_cnt:
            print("Empty frame")
            return

        np.save(os.path.join(self.masks_path, f'frame_{self.frame_num}_{mask_id}.npy'), fsz_mask)

        self.point_ids.append((self.frame_num, mask_id))
        self.saved_points.loc[f"{self.frame_num}_{mask_id}"] = [*top_left, *top_right, *bot_left, *bot_right]
        self.class_match[self.frame_num].append({mask_id: class_id})

    def __get_smoothed_points(self):

        def center(top_left, bot_right, bot_left, top_right):
            return ((top_left + bot_right) / 2 + (bot_left + top_right) / 2) / 2

        mind = pd.MultiIndex.from_tuples(self.point_ids, names=('frame_num', 'mask_id'))
        self.saved_points.index = mind
        saved_corners = self.saved_points.copy(deep=True)

        smooth_df = pd.DataFrame(columns=['x_top_left', 'y_top_left', 'x_top_right', 'y_top_right',
                                          'x_bot_left', 'y_bot_left', 'x_bot_right', 'y_bot_right'])

        while saved_corners.shape[0]:
            smooth_idx = []

            prev_frame_num = saved_corners.index[0]
            prev_points = saved_corners.loc[prev_frame_num]
            prev_center_x = center(prev_points[0], prev_points[6], prev_points[4], prev_points[2])
            prev_center_y = center(prev_points[1], prev_points[7], prev_points[5], prev_points[3])

            saved_corners.drop(prev_frame_num, inplace=True)

            smooth_df.loc[prev_frame_num[0]] = list(prev_points)
            smooth_idx.append(prev_frame_num)

            for frame_num, points in saved_corners.iterrows():
                if frame_num[0] - prev_frame_num[0] == 1:
                    center_x = center(points[0], points[6], points[4], points[2])
                    center_y = center(points[1], points[7], points[5], points[3])
                    dist = distance.euclidean([prev_center_x, prev_center_y], [center_x, center_y])
                    if dist < 30:
                        smooth_df.loc[frame_num[0]] = list(points)
                        smooth_idx.append(frame_num)
                        saved_corners.drop(frame_num, inplace=True)

                        prev_center_x = center_x
                        prev_center_y = center_y
                        prev_frame_num, prev_points = frame_num, points

                elif frame_num[0] - prev_frame_num[0] > 1:
                    break

            smooth_df = smooth_points(smooth_df)
            smooth_idx = pd.MultiIndex.from_tuples(smooth_idx, names=('frame_num', 'mask_id'))
            smooth_df.index = smooth_idx

            self.saved_points.loc[smooth_idx] = smooth_df
            smooth_df.drop(smooth_idx, inplace=True)

    def __load_points(self):
        '''
        The method loads smoothed points
        '''
        if self.load_smooth and self.config['source_type'] == 0:
            self.__get_smoothed_points()
            self.load_smooth = False

        row = np.array(self.saved_points.loc[(self.frame_num - 1, self.mask_id)])

        self.corners = np.split(row, 4)

    def insert_logo(self):
        '''
        This method insert logo into detected area on the frame
        '''
        # load logo
        if not self.detection_successful:
            return

        frame_num = self.frame_num - 1
        matching = self.class_match[frame_num]

        for match in matching:
            self.mask_id, banner_id = match.popitem()
            mask = np.load(os.path.join(self.masks_path, f'frame_{frame_num}_{self.mask_id}.npy'))
            logo = cv2.imread(self.replace[banner_id], cv2.IMREAD_UNCHANGED)
            self.__load_points()
            logo = self.__logo_color_adj(logo)
            transformed_logo = self.__adjust_logo_shape(logo)
            points = np.argwhere(mask == 1)
            for i, j in points:
                self.frame[i, j] = transformed_logo[i, j]
        del self.class_match[frame_num]

    def __adjust_logo_shape(self, logo):

        # points before and after transformation
        # top_left, bot_left, bot_right, top_right
        h, w = logo.shape[:2]
        pts1 = np.float32([(0, 0), (0, (h - 1)), ((w - 1), (h - 1)), ((w - 1), 0)])
        pts2 = np.float32([self.corners[0], self.corners[2], self.corners[3], self.corners[1]])

        # perspective transformation
        mtrx = cv2.getPerspectiveTransform(pts1, pts2)
        transformed_logo = cv2.warpPerspective(logo, mtrx, (self.frame.shape[1], self.frame.shape[0]), borderMode=1)

        return transformed_logo

    def __logo_color_adj(self, logo):

        # select banner area
        banner = self.frame[int(self.corners[0][1]):int(self.corners[2][1]),
                 int(self.corners[0][0]):int(self.corners[1][0])].copy()

        # get logo hsv
        logo_hsv = cv2.cvtColor(logo, cv2.COLOR_BGR2HSV)
        logo_h, logo_s, logo_v = np.transpose(logo_hsv, (2, 0, 1))

        # get banner hsv
        banner_hsv = cv2.cvtColor(banner, cv2.COLOR_BGR2HSV)
        _, banner_s, _ = np.transpose(banner_hsv, (2, 0, 1))

        # find the saturation difference between both images
        mean_logo_s = np.mean(logo_s).astype(int)
        mean_banner_s = np.mean(banner_s).astype(int)
        trans_coef = round(mean_banner_s / mean_logo_s, 2)

        # adjust logo saturation according to the difference
        adjusted_logo_s = (logo_s * trans_coef).astype('uint8')
        adjusted_logo_hsv = np.array([logo_h, adjusted_logo_s, logo_v]).transpose((1, 2, 0))
        adjusted_logo = cv2.cvtColor(adjusted_logo_hsv, cv2.COLOR_HSV2BGR)

        return adjusted_logo
