from scripts.check_runtime_matrix import check


def test_runtime_version_declarations_are_consistent():
    assert check() == []
