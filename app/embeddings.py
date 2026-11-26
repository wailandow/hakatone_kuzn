"""FastEmbed E5 passages and queries, with an injectable backend for offline tests."""
from threading import Lock

import numpy as np


MODEL_NAME = 'intfloat/multilingual-e5-small'
VECTOR_DTYPE = np.dtype('<f4')
_registry_lock = Lock()


def _fastembed_backend():
    try:
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType
    except ImportError as exc:
        raise RuntimeError('FastEmbed/ONNX Runtime could not be loaded; check the local runtime dependencies '
                           'or use --fts-only to build without vectors') from exc

    # Some FastEmbed releases do not list E5; register the upstream ONNX export.
    with _registry_lock:
        if not any(item['model'].casefold() == MODEL_NAME for item in TextEmbedding.list_supported_models()):
            TextEmbedding.add_custom_model(model=MODEL_NAME, pooling=PoolingType.MEAN,
                                           normalization=True, sources=ModelSource(hf=MODEL_NAME),
                                           model_file='onnx/model.onnx', dim=384)
    return TextEmbedding(model_name=MODEL_NAME, providers=['CPUExecutionProvider'])


class E5Embedding:
    def __init__(self, *, backend=None, batch_size=32):
        if batch_size < 1:
            raise ValueError('batch_size must be positive')
        self._backend = backend
        self._batch_size = batch_size

    def _encode(self, prefix, texts):
        documents = [prefix + text for text in texts]
        if not documents:
            return []
        if self._backend is None:
            self._backend = _fastembed_backend()
        vectors = [np.asarray(vector, dtype=VECTOR_DTYPE)
                   for vector in self._backend.embed(documents, batch_size=self._batch_size)]
        if len(vectors) != len(documents) or any(
            vector.ndim != 1 or not vector.size or vector.shape != vectors[0].shape
            or not np.isfinite(vector).all() or not vector.any() for vector in vectors
        ):
            raise ValueError('embedding backend returned invalid vectors')
        return vectors

    def passages(self, texts):
        return self._encode('passage: ', texts)

    def query(self, text):
        return self._encode('query: ', [text])[0]
