## Parent integration

Merged the PR135 continuation into the PR95 integration branch, then current main including recovery/context fixes and PR136 roster restoration. Preserved all histories and saved source work.

Found and fixed a production integration regression: the selected channel tool had been staged into a session directory, which the committed native extension loader rejects. It now ships inside the verified bundle and its existing deployment manifest. Removed the obsolete staging implementation. The actual compiled Pi resource loader loads the packaged tool and rejects an otherwise identical outside copy.

Parent checks so far: 149 combined runtime/recovery/roster/native tests passed (six optional fixtures skipped); 13 actual SDK/RPC ordinary compaction cases passed; 29 selected tool and launcher tests passed; two actual native-loader cases passed. Private compaction cases and installed/live activation are in progress. CI is deferred.

Private SDK/RPC shard: four passed, four deliberately inapplicable synthetic/private combinations skipped. Normal native preparation succeeded with the packaged tool and updated full artifact commitment. Existing current main recovery/context/roster changes are present in the integrated tree. New integration behavior uses the existing package manifest and tool boundary; no second tool engine or admission store was added.
