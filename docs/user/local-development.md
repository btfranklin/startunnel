# Local development

```shell
pdm run python scripts/bootstrap_env.py
pdm run dev
```

The startup wrapper selects an available host port and prints the final URL. The `web` log reports container port 8000; use the printed host URL.

For administration without a browser, create the first administrator and key:

```shell
docker compose exec -T web /app/.venv/bin/python manage.py create_instance_admin USERNAME --key-file /tmp/startunnel-admin.key
```

The file is inside the container. Transfer it through a private channel to the
CLI machine, keep mode `0600`, and remove the temporary container copy. Follow
the [admin quickstart](/docs/admin-quickstart/) to verify access.

For interactive browser-password setup, omit both `-T` and `--key-file`. Choose
one mode for a new account. Existing administrators can add keys or a password
through the product interfaces later.

Run local checks with `pdm run check`. Run real PostgreSQL proof with:

```shell
pdm run python scripts/run_isolated_test_lane.py postgres
```
