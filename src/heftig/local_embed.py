"""The built-in embedding model for the search by meaning: ONNX on the CPU, no server.

The model is downloaded once, when documents are embedded for the first time, from Hugging Face
at a fixed revision; every file is checked against its SHA-256 before it is used. It is stored in
``<archive>/models/`` (not part of backups or exports: it can always be downloaded again).
Inference uses onnxruntime and the Hugging Face tokenizer, both small native wheels - no torch.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import N_
from .providers.base import ProviderError, ProviderUnavailable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelSpec:
    name: str  # stored with every vector: vectors of another model are not compared
    repo: str
    revision: str
    files: dict[str, str]  # local name -> path in the repository
    sha256: dict[str, str] = field(default_factory=dict)  # local name -> expected hash
    pooling: str = "mean"  # "mean" or "cls"
    query_prefix: str = ""
    document_prefix: str = ""
    max_tokens: int = 512
    size_mb: int = 0
    label: str = ""
    min_similarity: float = 0.0  # cosine below which texts count as unrelated (calibrated)


# Chosen by measurement (docs/search.md, "Search by meaning"): on the German benchmark it ranks
# best by meaning alone (MRR 0.98; multilingual-e5-small 0.85, granite-97m 0.74), multilingual,
# Apache-2.0, int8-quantised for the CPU (about 17 chunks/s on two cores of a notebook CPU).
DEFAULT = ModelSpec(
    name="snowflake-arctic-embed-m-v2.0-int8",
    repo="Snowflake/snowflake-arctic-embed-m-v2.0",
    revision="95c2741480856aa9666782eb4afe11959938017f",
    files={"model.onnx": "onnx/model_int8.onnx", "tokenizer.json": "tokenizer.json"},
    sha256={
        "model.onnx": "03d923bb1850ebdccb068e2f3abd8aa43fe81c50d07d037ef103fe3d0fb78e3b",
        "tokenizer.json": "f1cc44ad7faaeec47241864835473fd5403f2da94673f3f764a77ebcb0a803ec",
    },
    pooling="cls",
    query_prefix="query: ",
    max_tokens=512,
    size_mb=330,
    label="Snowflake Arctic Embed M v2.0",
    min_similarity=0.22,
)


class LocalEmbedder:
    """Embedder protocol (providers/base.py) on a local ONNX model."""

    adapter_version = "local-onnx-v1"
    target = "local"
    BATCH = 16

    def __init__(self, spec: ModelSpec, directory: Path, threads: int | None = None):
        self.spec = spec
        self.name = "local"
        self.model = spec.name
        self.directory = directory / spec.name.replace("/", "--")
        self.threads = threads
        self._session = None
        self._tokenizer = None
        self._lock = threading.Lock()

    # --- files ---------------------------------------------------------------------------

    def downloaded(self) -> bool:
        return all((self.directory / f).exists() for f in self.spec.files)

    def download(self, progress=None) -> None:
        """Fetch the model files that are missing (atomically, hash checked)."""
        import httpx

        self.directory.mkdir(parents=True, exist_ok=True)
        for local, remote in self.spec.files.items():
            target = self.directory / local
            if target.exists():
                continue
            url = f"https://huggingface.co/{self.spec.repo}/resolve/{self.spec.revision}/{remote}"
            tmp = target.with_name(f".{local}.part")
            digest = hashlib.sha256()
            log.info("downloading %s", url)
            try:
                with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
                    if r.status_code != 200:
                        raise ProviderError(
                            N_("Downloading the search model failed (HTTP %(status)s).")
                            % {"status": r.status_code},
                            transient=True,
                        )
                    with open(tmp, "wb") as f:
                        for chunk in r.iter_bytes(1 << 20):
                            f.write(chunk)
                            digest.update(chunk)
                            if progress:
                                progress(local, f.tell())
            except httpx.HTTPError as e:
                tmp.unlink(missing_ok=True)
                raise ProviderError(
                    N_("Downloading the search model failed (%(error)s).")
                    % {"error": type(e).__name__},
                    transient=True,
                ) from e
            expected = self.spec.sha256.get(local)
            if expected and digest.hexdigest() != expected:
                tmp.unlink(missing_ok=True)
                raise ProviderError(N_("The downloaded search model is damaged (checksum)."))
            os.replace(tmp, target)

    # --- inference -------------------------------------------------------------------------

    def _load(self):
        with self._lock:
            if self._session is None:
                if not self.downloaded():
                    raise ProviderUnavailable(N_("The search model is not downloaded yet."))
                import onnxruntime as ort
                from tokenizers import Tokenizer

                opts = ort.SessionOptions()
                opts.intra_op_num_threads = self.threads or max(1, (os.cpu_count() or 2) // 2)
                opts.inter_op_num_threads = 1
                self._session = ort.InferenceSession(
                    str(self.directory / "model.onnx"), opts, providers=["CPUExecutionProvider"]
                )
                tok = Tokenizer.from_file(str(self.directory / "tokenizer.json"))
                tok.enable_truncation(max_length=self.spec.max_tokens)
                tok.enable_padding()
                self._tokenizer = tok
        return self._session, self._tokenizer

    def embed(self, texts: list[str]) -> list[list[float]]:
        import numpy as np

        session, tok = self._load()
        names = {i.name for i in session.get_inputs()}
        out: list[list[float]] = []
        # similar lengths in one batch: less padding
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        vectors: dict[int, list[float]] = {}
        for start in range(0, len(order), self.BATCH):
            idx = order[start : start + self.BATCH]
            enc = tok.encode_batch([texts[i] for i in idx])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in names:
                feed["token_type_ids"] = np.zeros_like(ids)
            result = session.run(None, feed)[0]
            if result.ndim == 3:  # token vectors: pool them
                if self.spec.pooling == "cls":
                    pooled = result[:, 0]
                else:
                    m = mask[..., None].astype(np.float32)
                    pooled = (result * m).sum(axis=1) / np.maximum(m.sum(axis=1), 1e-9)
            else:
                pooled = result
            for i, v in zip(idx, pooled, strict=True):
                vectors[i] = v.astype(np.float32).tolist()
        out = [vectors[i] for i in range(len(texts))]
        return out
