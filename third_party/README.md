# Third-party source

`wechatauto-replica` is included as a source snapshot so this project can be built without a nested Git repository. Its upstream project is <https://github.com/fanyuantaier/wechatauto-replica>; the snapshot is based on upstream `main` at commit `ad8035d0673d6e51debf6102c1010d56b6be78ce`.

The snapshot preserves the dependency's `LICENSE` and package metadata. It also includes local working-tree changes in `GUIDE.md`, the three README files, `docs/技术文档.md`, and `wechatauto/db.py`, `wechatauto/demo_forward.py`, `wechatauto/guia.py`, and `wechatauto/wx.py`. Example WeChat IDs in public examples and tests have been replaced with clearly synthetic `wxid_example_*` values. Review these changes before updating or redistributing the snapshot. The dependency is licensed separately under Apache-2.0. This notice does not set a license for the main application.
