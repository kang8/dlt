import pytest

from dlt.common.utils import flatten_list_of_str_or_dicts, digest128, map_nested_in_place


def test_flatten_list_of_str_or_dicts() -> None:
    l_d = [{"a": "b"}, "c", 1, [2]]
    d_d = flatten_list_of_str_or_dicts(l_d)
    assert d_d == {"a": "b", "c": None, "1": None, "[2]": None}
    # key clash
    l_d = [{"a": "b"}, "a"]
    with pytest.raises(KeyError):
        d_d = flatten_list_of_str_or_dicts(l_d)


def test_digest128_length() -> None:
    assert len(digest128("hash it")) == 120/6


def test_map_dicts_in_place() -> None:
    _d = {
        "a": "1",
        "b": ["a", "b", ["a", "b"], {"a": "c"}],
        "c": {
            "d": "e",
            "e": ["a", 2]
        }
    }
    exp_d = {'a': '11', 'b': ['aa', 'bb', ['aa', 'bb'], {'a': 'cc'}], 'c': {'d': 'ee', 'e': ['aa', 4]}}
    assert map_nested_in_place(lambda v: v*2, _d) == exp_d
    # in place
    assert _d == exp_d

    _l = ["a", "b", ["a", "b"], {"a": "c"}]
    exp_l = ["aa", "bb", ["aa", "bb"], {"a": "cc"}]
    assert map_nested_in_place(lambda v: v*2, _l) == exp_l
    assert _l == exp_l

    with pytest.raises(ValueError):
        map_nested_in_place(lambda v: v*2, "a")