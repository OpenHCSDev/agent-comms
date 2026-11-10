# Separate physical import insertions from declaration/body spans; preserve native claims.
def phase:
  if .operation == "create_file" then 0
  elif (.operation == "ensure_import" or .operation == "remove_import_names") then 1
  else 2 end;
def merged_patches:
  ([.[] | select(.operation != "patch_target")]
   + [.[] | select(.operation == "patch_target")]
     | . as $all
     | [$all[] | select(.operation != "patch_target")]
       + ([$all[] | select(.operation == "patch_target")]
          | group_by([.file_path, .target_qualname])
          | map(.[0] + {replacements: [.[].replacements[]]})));
{stages: [.stages[] as $stage
  | range(0;3) as $phase
  | $stage
  | .recipes |= map(
      .recipe_id += ("-physical-" + ($phase | tostring))
      | .operations |= (map(select(phase == $phase)) | merged_patches)
      | select((.operations | length) > 0))
  | select((.recipes | length) > 0)]}
