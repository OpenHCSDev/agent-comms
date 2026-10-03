# Native immutable resource sharing

Existing native_package owns bounded whole-tree content provenance. prepare-pi-native owns the sole compiled package publication; native-import-fence owns every SDK import. A hardlink is currently rejected even when it only shares identical frozen code. The existing tree commitment already records path/type/execute-mode/size/content rather than link count. Link count and ctime are also compared during hashing, so attaching another immutable name currently causes a false mutation refusal.

Migrate this complete artifact family: accept only readonly shared regular resources, preserve no-follow/owner/bounds/content/inode checks; have the existing builder seal resources only after all patching and reuse only a resource donor carrying the same newly reviewed import fence. Current89/f3dd/d5 artifacts have the old fence and cannot be donors. Private journals/settings/auth/source binding remain distinct single-link facts. No handed artifact changes.

Source lead: IMPL-13 (one filesystem boundary implemented at different levels). Before/after AST consumers and parse omissions are recorded for the bounded family; these do not prove dynamic execution. Checks come after the complete change. No provider/native schema or public change.
