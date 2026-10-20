"""Stored declarations cannot change without their owner's registered conversion."""
from __future__ import annotations

import importlib
from inspect import isabstract
import json
from pathlib import Path
import pkgutil
import subprocess

from dataclasses import dataclass

import pytest
import agent_comms
from agent_comms.field_codec import FieldCodec
from agent_comms.locked_store import DocumentConversion, LockedStore
from agent_comms.store_files import StoreOwner

MANIFEST = Path(__file__).with_name("stored_format_fingerprints.json")
ROOT = Path(__file__).resolve().parents[2]


def test_schema_sets_are_unordered_but_default_data_and_tuple_positions_are_not():
    normalize = LockedStore._canonical_schema
    assert normalize({"required": ["b", "a"]}) == normalize({"required": ["a", "b"]})
    assert normalize({"default": {"enum": [2, 1]}}) != normalize({"default": {"enum": [1, 2]}})
    assert normalize({"prefixItems": [{"type": "string"}, {"type": "number"}]}) != normalize(
        {"prefixItems": [{"type": "number"}, {"type": "string"}]})
