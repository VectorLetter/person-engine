"""嵌入模型统一接口与两个实现（SFace / AuraFace）。

约定：所有 embed() 返回 L2 归一化的 float32 向量；
不同模型的向量永不混用、永不直接比较。
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
import onnxruntime as ort


class Embedder(Protocol):
    name: str
    model_version: str

    def align_crop(self, img_bgr: np.ndarray, face_row: np.ndarray) -> np.ndarray:
        """从原图+检测行产出对齐裁片（112×112 BGR）。"""
        ...

    def embed(self, aligned_bgr: np.ndarray) -> np.ndarray:
        """对齐裁片 → 归一化 embedding。"""
        ...


class SFaceEmbedder:
    name = "sface"
    model_version = "2021dec"

    def __init__(self, model_path: str | Path):
        self._rec = cv2.FaceRecognizerSF.create(
            model=str(model_path), config="", backend_id=0, target_id=0)

    def align_crop(self, img_bgr: np.ndarray, face_row: np.ndarray) -> np.ndarray:
        # face_row 为 YuNet 15 元素行；官方 alignCrop 消费 bbox+landmark（前 14 项）
        return self._rec.alignCrop(img_bgr, face_row[:14])

    def embed(self, aligned_bgr: np.ndarray) -> np.ndarray:
        feat = self._rec.feature(aligned_bgr).ravel().astype(np.float32)
        return feat / np.linalg.norm(feat)


class AuraFaceEmbedder:
    name = "auraface"
    model_version = "glintr100-v1"
    # 预处理采用 InsightFace 惯例（RGB, (x-127.5)/127.5, 112×112）。
    # 模型卡未明确声明 —— 以 smoke test / benchmark 实测校准为准。

    def __init__(self, model_path: str | Path):
        self._sess = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"])
        self._input_name = self._sess.get_inputs()[0].name
        in_shape = self._sess.get_inputs()[0].shape  # e.g. [1,3,112,112]（可能含动态维）
        self.input_spec = str(in_shape)
        out_shape = self._sess.get_outputs()[0].shape
        self.output_spec = str(out_shape)

    def align_crop(self, img_bgr: np.ndarray, face_row: np.ndarray) -> np.ndarray:
        from .align import align_face
        from .detect import YuNetDetector
        return align_face(img_bgr, YuNetDetector.landmarks5(face_row))

    def embed(self, aligned_bgr: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2RGB)
        x = rgb.astype(np.float32)
        x = (x - 127.5) / 127.5
        x = x.transpose(2, 0, 1)[np.newaxis, ...]  # 1×3×112×112
        out = self._sess.run(None, {self._input_name: x})[0]
        v = out.ravel().astype(np.float32)
        return v / np.linalg.norm(v)
