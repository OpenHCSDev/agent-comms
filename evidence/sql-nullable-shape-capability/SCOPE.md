# Explicit SQL nullability for nominal scalar shapes

Parent owns the existing TypedTable/Column capability requested by Arendt470.
Arendt owns NativeAdmissionEpoch and the complete original producer/readers.
Column nullable declaration describes SQLite storage, independently of whether
the semantic value is optional. Original FieldCodec JsonShapeFamily decodes SQL
NULL/integer once into the declared behavior-owning family. No storage or codec
subclass, schema migration, adapter, mirrored state or public mutation.

Change Column and its derived field nullability only. Check actual SQLite DDL,
insert/read/update round trips for mandatory semantic null/integer states,
rejection of invalid primitive shapes, and existing Optional/NOT NULL columns.
This shared boundary proof does not establish installed native lifecycle
readiness; Arendt470 owns the actual affected integrated recovery/cancel path.
