"""Train the fixed v1 neural two-tower retriever.

The sampled-softmax candidate set combines in-batch items (corrected for their
popularity-skewed sampling probability) with full-catalog uniform negatives. Known
user positives are masked, so an observed interaction is never taught as a negative.
All computation is CPU, single-threaded, seeded, and deterministic.

Run: ``python -m sift.retrieval.two_tower``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
import numpy as np
import torch
from numpy.typing import NDArray
from scipy.sparse import csr_matrix  # type: ignore[import-untyped]
from torch import Tensor, nn
from torch.nn import functional as F

from sift.config import SPLIT_T
from sift.features.definitions import (
    BEHAVIORAL_EMBEDDING_DIM,
    ITEM_TWO_TOWER_EMBEDDING,
    USER_TWO_TOWER_EMBEDDING,
    get_embedding,
)
from sift.retrieval.als import write_factor_parquet
from sift.retrieval.interactions import InteractionData, load_interactions
from sift.retrieval.two_tower_data import (
    TWO_TOWER_DIR,
    USER_FEATURES,
    ItemInputs,
    TrainingExamples,
    load_item_inputs,
    load_training_examples,
)
from sift.store.read import asof_feature_query, attach_store

OUTPUT_DIM = BEHAVIORAL_EMBEDDING_DIM
ID_DIM = 32
HIDDEN_DIM = 128
BATCH_SIZE = 512
UNIFORM_NEGATIVES = 128
EPOCHS = 5
LEARNING_RATE = 1e-3
WEIGHT_DECAY = 1e-5
TEMPERATURE = 0.07
SEED = 42

USER_FACTOR_PARQUET = get_embedding(USER_TWO_TOWER_EMBEDDING).artifact
ITEM_FACTOR_PARQUET = get_embedding(ITEM_TWO_TOWER_EMBEDDING).artifact
USER_FACTORS = USER_FACTOR_PARQUET.with_suffix(".npy")
ITEM_FACTORS = ITEM_FACTOR_PARQUET.with_suffix(".npy")
USER_IDS = TWO_TOWER_DIR / "user_ids.json"
ITEM_IDS = TWO_TOWER_DIR / "item_ids.json"
MANIFEST = TWO_TOWER_DIR / "manifest.json"


@dataclass(frozen=True)
class UserNormalization:
    log_count_mean: float
    log_count_std: float
    log_days_mean: float
    log_days_std: float


def _safe_std(values: NDArray[np.float32]) -> float:
    value = float(np.std(values, dtype=np.float64))
    return value if value > 1e-8 else 1.0


def fit_user_normalization(values: NDArray[np.float32]) -> UserNormalization:
    log_count = np.log1p(np.nan_to_num(values[:, 0], nan=0.0))
    log_days = np.log1p(np.nan_to_num(values[:, 2], nan=0.0))
    return UserNormalization(
        log_count_mean=float(np.mean(log_count, dtype=np.float64)),
        log_count_std=_safe_std(log_count),
        log_days_mean=float(np.mean(log_days, dtype=np.float64)),
        log_days_std=_safe_std(log_days),
    )


def transform_user_values(
    values: NDArray[np.float32], normalization: UserNormalization
) -> NDArray[np.float32]:
    """Five finite inputs: two standardized values, rating, and missing flags."""
    count = np.log1p(np.nan_to_num(values[:, 0], nan=0.0))
    mean_present = np.isfinite(values[:, 1]).astype(np.float32)
    mean_stars = np.nan_to_num(values[:, 1], nan=0.0) / 5.0
    days_present = np.isfinite(values[:, 2]).astype(np.float32)
    days = np.log1p(np.nan_to_num(values[:, 2], nan=0.0))
    return np.column_stack(
        [
            (count - normalization.log_count_mean) / normalization.log_count_std,
            mean_stars,
            mean_present,
            (days - normalization.log_days_mean) / normalization.log_days_std,
            days_present,
        ]
    ).astype(np.float32, copy=False)


def transform_item_dense(values: NDArray[np.float32]) -> NDArray[np.float32]:
    out = values.copy()
    for column in (0, 1):
        mean = float(np.mean(out[:, column], dtype=np.float64))
        std = _safe_std(out[:, column])
        out[:, column] = (out[:, column] - mean) / std
    return out


class TwoTower(nn.Module):
    """ID embeddings plus independently-computable side features on each tower."""

    def __init__(self, n_users: int, n_items: int, n_categories: int) -> None:
        super().__init__()
        self.user_ids = nn.Embedding(n_users, ID_DIM)
        self.item_ids = nn.Embedding(n_items, ID_DIM)
        self.user_mlp = nn.Sequential(
            nn.Linear(ID_DIM + 5, HIDDEN_DIM),
            nn.ReLU(),
            nn.Linear(HIDDEN_DIM, OUTPUT_DIM),
        )
        self.item_mlp = nn.Sequential(
            nn.Linear(ID_DIM + 4 + n_categories, HIDDEN_DIM),
            nn.ReLU(),
            nn.Linear(HIDDEN_DIM, OUTPUT_DIM),
        )

    def encode_users(self, indices: Tensor, dense: Tensor) -> Tensor:
        return F.normalize(self.user_mlp(torch.cat([self.user_ids(indices), dense], dim=1)))

    def encode_items(self, indices: Tensor, dense: Tensor, categories: Tensor) -> Tensor:
        inputs = torch.cat([self.item_ids(indices), dense, categories], dim=1)
        return F.normalize(self.item_mlp(inputs))


def known_positive_mask(
    history: csr_matrix,
    user_indices: NDArray[np.int64],
    candidate_items: NDArray[np.int64],
    batch_positives: int,
) -> NDArray[np.bool_]:
    """Mask observed positives except each row's diagonal training target."""
    mask = np.asarray(history[user_indices][:, candidate_items].toarray(), dtype=np.bool_)
    mask[np.arange(batch_positives), np.arange(batch_positives)] = False
    return mask


def load_export_user_values(
    user_ids: tuple[str, ...],
    item_ids: tuple[str, ...],
) -> NDArray[np.float32]:
    """Read every known user's tower inputs as-of frozen T through the store."""
    con = duckdb.connect()
    try:
        attach_store(con)
        con.register("export_users", np.asarray(user_ids))
        con.execute(
            "CREATE TABLE queries AS SELECT row_number() OVER () - 1 AS query_id, "
            "column0 AS user_id, ? AS business_id, ?::TIMESTAMP AS ts FROM export_users",
            [item_ids[0], str(SPLIT_T)],
        )
        values = con.execute(asof_feature_query(USER_FEATURES)).fetchnumpy()
    finally:
        con.close()
    return np.column_stack(
        [
            np.asarray(np.ma.filled(np.ma.asarray(values[name], dtype=np.float32), np.nan))
            for name in USER_FEATURES
        ]
    ).astype(np.float32, copy=False)


def _encode_all(
    model: TwoTower,
    user_values: NDArray[np.float32],
    item_inputs: ItemInputs,
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    item_dense = transform_item_dense(item_inputs.dense)
    users: list[NDArray[np.float32]] = []
    items: list[NDArray[np.float32]] = []
    model.eval()
    with torch.no_grad():
        for start in range(0, len(user_values), 4096):
            stop = min(start + 4096, len(user_values))
            encoded = model.encode_users(
                torch.arange(start, stop), torch.from_numpy(user_values[start:stop])
            )
            users.append(encoded.numpy(force=True).astype(np.float32, copy=False))
        for start in range(0, len(item_dense), 4096):
            stop = min(start + 4096, len(item_dense))
            encoded = model.encode_items(
                torch.arange(start, stop),
                torch.from_numpy(item_dense[start:stop]),
                torch.from_numpy(item_inputs.categories[start:stop]),
            )
            items.append(encoded.numpy(force=True).astype(np.float32, copy=False))
    return np.concatenate(users), np.concatenate(items)


def train_two_tower(
    examples: TrainingExamples,
    interactions: InteractionData,
    item_inputs: ItemInputs,
    export_user_raw: NDArray[np.float32],
    *,
    out_dir: Path = TWO_TOWER_DIR,
    epochs: int = EPOCHS,
    progress: bool = True,
) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
    """Train once and export normalized user/item vectors plus a JSON manifest."""
    torch.set_num_threads(1)
    torch.manual_seed(SEED)
    torch.use_deterministic_algorithms(True)
    generator = torch.Generator().manual_seed(SEED)

    normalization = fit_user_normalization(examples.user_values)
    train_users = transform_user_values(examples.user_values, normalization)
    export_users = transform_user_values(export_user_raw, normalization)
    item_dense = transform_item_dense(item_inputs.dense)
    n_users, n_items = interactions.matrix.shape
    model = TwoTower(n_users, n_items, len(item_inputs.category_names))
    optimizer = torch.optim.AdamW(model.parameters(), lr=LEARNING_RATE, weight_decay=WEIGHT_DECAY)
    item_counts = np.bincount(examples.item_indices, minlength=n_items).astype(np.float64)
    item_q = item_counts / len(examples.item_indices)

    user_indices = torch.from_numpy(examples.user_indices)
    item_indices = torch.from_numpy(examples.item_indices)
    user_dense = torch.from_numpy(train_users)
    static_dense = torch.from_numpy(item_dense)
    static_categories = torch.from_numpy(item_inputs.categories)
    history = interactions.matrix.astype(bool).tocsr()

    model.train()
    for epoch in range(epochs):
        order = torch.randperm(len(user_indices), generator=generator)
        loss_sum = 0.0
        rows = 0
        for start in range(0, len(order), BATCH_SIZE):
            selected = order[start : start + BATCH_SIZE]
            batch_size = len(selected)
            if batch_size < 2:
                continue
            batch_users = user_indices[selected]
            positives = item_indices[selected]
            uniform = torch.randint(
                n_items, (UNIFORM_NEGATIVES,), generator=generator, dtype=torch.int64
            )
            candidates = torch.cat([positives, uniform])
            user_vectors = model.encode_users(batch_users, user_dense[selected])
            item_vectors = model.encode_items(
                candidates,
                static_dense[candidates],
                static_categories[candidates],
            )
            logits = user_vectors @ item_vectors.T / TEMPERATURE
            candidate_np = candidates.numpy(force=True)
            mixture_q = (batch_size * item_q[candidate_np] + UNIFORM_NEGATIVES / n_items) / (
                batch_size + UNIFORM_NEGATIVES
            )
            logits = logits - torch.from_numpy(np.log(mixture_q)).to(logits.dtype)
            mask = known_positive_mask(
                history,
                batch_users.numpy(force=True),
                candidate_np,
                batch_size,
            )
            logits.masked_fill_(torch.from_numpy(mask), -torch.inf)
            loss = F.cross_entropy(logits, torch.arange(batch_size))
            optimizer.zero_grad(set_to_none=True)
            loss.backward()  # type: ignore[no-untyped-call]
            optimizer.step()
            loss_sum += float(loss.detach()) * batch_size
            rows += batch_size
        if progress:
            print(f"epoch {epoch + 1}/{epochs} sampled-softmax loss={loss_sum / rows:.5f}")

    user_factors, item_factors = _encode_all(model, export_users, item_inputs)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "user_npy": out_dir / USER_FACTORS.name,
        "item_npy": out_dir / ITEM_FACTORS.name,
        "user_parquet": out_dir / USER_FACTOR_PARQUET.name,
        "item_parquet": out_dir / ITEM_FACTOR_PARQUET.name,
        "user_ids": out_dir / USER_IDS.name,
        "item_ids": out_dir / ITEM_IDS.name,
        "manifest": out_dir / MANIFEST.name,
    }
    np.save(paths["user_npy"], user_factors, allow_pickle=False)
    np.save(paths["item_npy"], item_factors, allow_pickle=False)
    paths["user_ids"].write_text(json.dumps(interactions.user_ids))
    paths["item_ids"].write_text(json.dumps(interactions.item_ids))
    write_factor_parquet(
        interactions.user_ids,
        user_factors,
        id_column="user_id",
        out_file=paths["user_parquet"],
    )
    write_factor_parquet(
        interactions.item_ids,
        item_factors,
        id_column="business_id",
        out_file=paths["item_parquet"],
    )
    paths["manifest"].write_text(
        json.dumps(
            {
                "name": "embedding_two_tower_v1",
                "output_dimensions": OUTPUT_DIM,
                "id_dimensions": ID_DIM,
                "hidden_dimensions": HIDDEN_DIM,
                "batch_size": BATCH_SIZE,
                "uniform_negatives": UNIFORM_NEGATIVES,
                "epochs": epochs,
                "learning_rate": LEARNING_RATE,
                "weight_decay": WEIGHT_DECAY,
                "temperature": TEMPERATURE,
                "seed": SEED,
                "users": n_users,
                "items": n_items,
                "positive_events": len(examples.user_indices),
                "categories": list(item_inputs.category_names),
                "user_normalization": asdict(normalization),
                "output_l2_normalized": True,
                "negative_sampling": "in_batch_plus_uniform_logq",
                "known_positives_masked": True,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return user_factors, item_factors


def main() -> None:
    interactions = load_interactions()
    examples = load_training_examples(interactions.user_ids, interactions.item_ids)
    items = load_item_inputs(interactions.item_ids)
    export_users = load_export_user_values(interactions.user_ids, interactions.item_ids)
    users, item_factors = train_two_tower(examples, interactions, items, export_users)
    print(f"user factors: {users.shape} -> {USER_FACTOR_PARQUET}")
    print(f"item factors: {item_factors.shape} -> {ITEM_FACTOR_PARQUET}")
    print(f"manifest: {MANIFEST}")


if __name__ == "__main__":
    main()
