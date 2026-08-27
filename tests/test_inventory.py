from pathlib import Path

import pytest

from te_agent_migrate.errors import ConfigurationError
from te_agent_migrate.inventory import load_inventory


def test_load_inventory_strict_columns(tmp_path: Path):
    path = tmp_path / "inventory.csv"
    path.write_text("hostname,ip_address\na,192.0.2.1\n")
    assert load_inventory(path)[0].hostname == "a"


def test_load_inventory_rejects_extra_columns(tmp_path: Path):
    path = tmp_path / "inventory.csv"
    path.write_text("hostname,ip_address,password\na,192.0.2.1,x\n")
    with pytest.raises(ConfigurationError):
        load_inventory(path)
