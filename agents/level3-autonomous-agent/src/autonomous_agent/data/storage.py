"""Delta Lake storage. Only the URI scheme changes between clouds."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pyarrow as pa
from deltalake import DeltaTable, write_deltalake
from deltalake.exceptions import TableNotFoundError

Layer = Literal["bronze", "silver", "gold"]


class LakehouseStorage:
    def __init__(self, root: str, storage_options: dict[str, str] | None = None) -> None:
        self.root = root.rstrip("/")
        self.options = storage_options or {}
        self.is_local = "://" not in root or root.startswith("file://")
        if self.is_local:
            Path(self.root.removeprefix("file://")).mkdir(parents=True, exist_ok=True)

    def uri(self, layer: Layer, table: str) -> str:
        return f"{self.root}/{layer}/{table}"

    def write(
        self,
        layer: Layer,
        table: str,
        data: pa.Table,
        *,
        mode: Literal["append", "overwrite"] = "append",
        partition_by: list[str] | None = None,
        replace_where: str | None = None,
        description: str | None = None,
    ) -> int:
        uri = self.uri(layer, table)
        options = self.options or None
        config = {"delta.appendOnly": "false"}
        if mode == "append":
            write_deltalake(
                uri,
                data,
                mode="append",
                partition_by=partition_by,
                schema_mode="merge",
                storage_options=options,
                description=description,
                configuration=config,
            )
        else:
            # replaceWhere-style overwrite keeps reruns idempotent per partition.
            write_deltalake(
                uri,
                data,
                mode="overwrite",
                partition_by=partition_by,
                predicate=replace_where,
                storage_options=options,
                description=description,
                configuration=config,
            )
        return self.version(layer, table)

    def read(self, layer: Layer, table: str) -> pa.Table | None:
        try:
            dt = DeltaTable(self.uri(layer, table), storage_options=self.options or None)
        except TableNotFoundError:
            return None
        result: pa.Table = dt.to_pyarrow_table()
        return result

    def version(self, layer: Layer, table: str) -> int:
        dt = DeltaTable(self.uri(layer, table), storage_options=self.options or None)
        return int(dt.version())

    def history(self, layer: Layer, table: str) -> list[dict[str, object]]:
        dt = DeltaTable(self.uri(layer, table), storage_options=self.options or None)
        return list(dt.history())
