# Pending submissions

Drop a file here in a pull request to submit a transaction.

```bash
python3 wallet.py send --to salt1... --amount 1.5 --memo "gg"
```

Put the `salt-tx-v1:` line it prints into a new file named after yourself,
for example `chain/pending/octocat-1.txt`, and open a pull request.

The node reads the file through the API, validates the transaction, applies
it to `main` itself and closes the pull request. It is never merged — which
is both why your submission cannot conflict with anyone else's, and why no
code from your fork is ever executed by the workflow.

Files in this directory are never merged, so it stays empty.
