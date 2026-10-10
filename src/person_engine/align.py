"""5 点相似变换对齐到 112×112（AuraFace 路径；SFace 路径用 cv.FaceRecognizerSF.alignCrop）。"""
from __future__ import annotations

import cv2
import numpy as np

# ArcFace 标准 112×112 模板，顺序 [左眼, 右眼, 鼻尖, 左嘴角, 右嘴角]
TEMPLATE_112 = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float64,
)


def align_face(img_bgr: np.ndarray, landmarks5: np.ndarray,
               size: int = 112) -> np.ndarray:
    """landmarks5 顺序须为 [le, re, nose, lm, rm]。返回 size×size BGR 裁片。"""
    src = np.asarray(landmarks5, dtype=np.float64)
    dst = TEMPLATE_112 * (size / 112.0)
    tmat, _ = cv2.estimateAffinePartial2D(src, dst, method=cv2.LMEDS)
    if tmat is None:
        raise RuntimeError("相似变换估计失败（landmark 退化）")
    return cv2.warpAffine(img_bgr, tmat, (size, size),
                          flags=cv2.INTER_LINEAR, borderValue=0)
