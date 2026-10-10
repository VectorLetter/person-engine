"""YuNet 人脸检测（OpenCV DNN）。输出 bbox + 5 点 landmark + 置信度。"""
from __future__ import annotations

import cv2
import numpy as np

# FaceDetectorYN 输出行：[x, y, w, h,
#   re_x, re_y, le_x, le_y, nose_x, nose_y, rm_x, rm_y, lm_x, lm_y, score]
# 共 15 个元素；[:14] 可直接喂给 FaceRecognizerSF.alignCrop。
LANDMARK_SLICES = {
    "right_eye": slice(4, 6),
    "left_eye": slice(6, 8),
    "nose": slice(8, 10),
    "right_mouth": slice(10, 12),
    "left_mouth": slice(12, 14),
}


class YuNetDetector:
    def __init__(self, model_path: str, conf_threshold: float = 0.6,
                 nms_threshold: float = 0.3, top_k: int = 5000):
        self._model = cv2.FaceDetectorYN.create(
            model=str(model_path), config="", input_size=(320, 320),
            score_threshold=conf_threshold, nms_threshold=nms_threshold, top_k=top_k,
        )

    def detect(self, img_bgr: np.ndarray) -> list[np.ndarray]:
        """返回检测到的人脸行列表（每行 15 元素，float32）。

        兼容 OpenCV 4.x（返回 (faces, retval)）与 5.x（返回 (retval, faces)）。
        """
        h, w = img_bgr.shape[:2]
        self._model.setInputSize((w, h))
        ret = self._model.detect(img_bgr)
        faces = None
        candidates = ret if isinstance(ret, tuple) else (ret,)
        for r in candidates:
            if hasattr(r, "shape") and getattr(r, "ndim", 0) == 2:
                faces = r
                break
        return [] if faces is None or len(faces) == 0 else [row for row in faces]

    @staticmethod
    def landmarks5(face_row: np.ndarray) -> np.ndarray:
        """返回 5 点坐标，顺序与 ArcFace 模板一一对应：

        [观众视角左眼, 右眼, 鼻尖, 左嘴角, 右嘴角]
        """
        return np.array([
            face_row[4:6],    # viewer-left eye   ↔ 模板 (38.3, 51.7)
            face_row[6:8],    # viewer-right eye  ↔ 模板 (73.5, 51.5)
            face_row[8:10],   # nose tip          ↔ 模板 (56.0, 71.7)
            face_row[10:12],  # viewer-left mouth ↔ 模板 (41.5, 92.4)
            face_row[12:14],  # viewer-right mouth↔ 模板 (70.7, 92.2)
        ], dtype=np.float64)
