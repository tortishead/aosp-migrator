# M4: running the analysis on your trees

The analysis is deterministic: it reads the source and target trees with git and spends no
Copilot credits. It needs only an ingested run (see `docs/spike-m3.md`, steps 1 to 5); the
Copilot steps of the spike are not required.

## Run it

```bash
migrate ingest -c ~/spike/project.yaml --no-jira    # or with Jira, for ticket links
migrate analyse -c ~/spike/project.yaml
migrate clusters -c ~/spike/project.yaml
migrate clusters -c ~/spike/project.yaml --show <cluster id>
```

`analyse` prints how many patches ended in each state, which rung decided them, and the
forecast of the clusters still to migrate. `clusters` lists the clusters in migration order;
`--show` prints the members, the links that joined them, the forecast reasons and the net diff.

A run can be analysed once. To try again (for example, after changing `analysis` settings or
path rules), ingest into a new run; intents are reused by fingerprint, so that costs nothing.

## What helps most to check

- **Upstream verdicts:** pick a few `upstream` patches per rung and confirm the evidence in
  `migrate clusters --show` (target commit for identity, the original commit for provenance,
  matched lines for textual). A wrong `upstream` drops a needed patch, so these matter most.
- **Obsolete verdicts:** confirm the file is really gone in the target and has no successor.
  Files that moved into a mainline module should show as `moved_project`, not deleted.
- **Clusters:** do the clusters match how your team thinks about the features? Note clusters
  that join unrelated work (often a shared hot file) or split one feature.
- **Fold errors:** a cluster whose chain doesn't replay on the base alone usually depends on a
  patch in another cluster. These are listed as warnings.
- **Time:** how long `analyse` took for how many patches and projects.

## Assumptions to confirm

- Both trees share history (the target's projects contain the source base commits). If not,
  the analysis compares contents instead, and fewer patches are decided automatically.
- `trees.source.base_ref` and `trees.target.base_ref` resolve in every project that has OEM
  patches. Projects where they don't are skipped with a warning.
