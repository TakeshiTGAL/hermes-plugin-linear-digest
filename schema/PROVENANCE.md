# Schema provenance

`schema.graphql` is `packages/sdk/src/schema.graphql` from the Linear repository
at commit `8397645bc0a496f68896b18ec6453dddd5e51927` (2026-10-28,
"chore(release): update schema and version packages"). That commit is the
schema update published with `@linear/sdk` 100.0.0.

The npm tarball `@linear/sdk@100.0.0` does not contain a `.graphql` file.
Checked tarballs 1.22.0, 7.0.1, 28.0.0, 39.0.0, 51.0.0, 70.0.0, 90.0.0, 99.0.0,
and 100.0.0. The schema file lives in the SDK package source, which is the
official schema the SDK is generated from.

The file is MIT, the same license as that repository. `schema/NOTICE` is the
LICENSE text from that repository, including `Copyright (c) 2019 Linear` and
the permission notice. `schema.graphql` itself is unchanged. The morning job
does not download it.
