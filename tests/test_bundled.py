"""Locations of data files shipped inside the postmortem package."""

from __future__ import annotations

from postmortem.bundled import bundled_dungeon_data_path, bundled_interrupt_data_path


class TestBundledDataPaths:
    def test_dungeon_data_path_resolves_next_to_this_package_and_exists(self):
        path = bundled_dungeon_data_path()
        assert path.name == "dungeon_data.json"
        assert path.parent.name == "data"
        assert path.is_file()  # this repo ships a real copy

    def test_interrupt_data_path_resolves_next_to_this_package_and_exists(self):
        path = bundled_interrupt_data_path()
        assert path.name == "interrupt_data.json"
        assert path.parent.name == "data"
        assert path.is_file()  # this repo ships a real copy

    def test_both_paths_sit_in_the_same_data_folder(self):
        assert bundled_dungeon_data_path().parent == bundled_interrupt_data_path().parent
