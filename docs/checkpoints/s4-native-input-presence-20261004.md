# Original captured probe input presence

## What changed

RecordedNativeProbe now measures the rendered native user public text inside the original captured SDK message segments. It decodes each message once through PiMessage and uses UserMessage.matches_input for exact text and, separately, original input-ID matches. NativeMessages owns which segments contain provider messages. Original segment/message coordinates stay visible. System instructions, tool descriptions, assistant echoes and fragments spread across user messages cannot count as an exact user message.

Positive matches remain observable beside opaque content. Without a positive match, an unknown role/content prevents determining absence: the result stays unavailable. Missing captured request bytes also stay unavailable. Duplicate historical text can establish text presence only; without an original ID match it does not identify the fresh input. Images and unrepresented content are not measured by public text.

The existing serialized_construction boundary now also compares decoded SDK values with original captured JSON values, after its unchanged byte-length/digest checks. This closes reuse of inconsistent decoded objects for presence/condition consumers. JSON comparison preserves boolean/number kinds rather than Python's true==1 equality. It does not replace original byte evidence or claim HTTP bytes.

## Owners and consumers

No new class, store, cache, codec, native observer or runtime method. RecordedNativeProbe.read publishes this observation alongside retained-envelope presence. ScoredScenario.condition_construction groups the same acquired observations against every frozen round: present, known absent and unavailable rounds remain distinct. Existing individual/public, paired and CLI consumers derive from those owners. Overall condition construction/study acceptance remains false.

Existing refactor-audit Package before/after output covers 734 Python src/tests/tools modules with no parse omissions. It is lexical source evidence, not JavaScript or dynamic dispatch proof. Three private files change:124 additions/4 deletions. Runtime, stack and tools have zero change relative to main62b0f706; W1/native/import/lifecycle owners remain untouched.

## Final checks and limits

One affected batch after implementation:5PASS/.35s, covering exact Unicode/text-array input, original coordinates, input-ID distinction, assistant/system/tool exclusions, split-message refusal, opaque absence, missing bytes, original value contradiction and existing condition/scorer consumers. The first batch's missing control import and raw negative are preserved. Existing paired CLI ran once on two empty recorded trajectories:both arms keep all three rounds unavailable; no study result or stderr.

[Original receipt and eight raw hashes](../../evidence/s4-native-input-presence-20261004/receipt.json). Full CLI stdout stays at the named persistent raw root; published selection retains only the relevant original fields. No original session/SDK/provider/native, package/env/holder operation. No configured submission, full transformed-content/capacity, HTTP/provider receipt, intervention, recall/billing or full S4 acceptance claim. The separate30-pair/USD75 study remains unapproved; this checkpoint launches none.
