#!/bin/sh
set -eu

acl_dir=/var/lib/falkordb/acl
acl_file="$acl_dir/users.acl"

for variable in \
    FALKORDB_ADMIN_PASSWORD \
    FALKORDB_READER_ACME_PASSWORD \
    FALKORDB_READER_GLOBEX_PASSWORD \
    FALKORDB_WRITER_ACME_PASSWORD \
    FALKORDB_WRITER_GLOBEX_PASSWORD; do
    eval "value=\${$variable:-}"
    if [ -z "$value" ]; then
        echo "missing required database credential: $variable" >&2
        exit 64
    fi
done

umask 077
mkdir -p "$acl_dir"
temporary_acl="$acl_dir/users.acl.tmp"

# Graph names are Redis ACL keys. The readers can inspect only shared demo
# graphs, their own memory graph, and their dedicated probe graph. Writers do
# not receive company_brain access, graph enumeration, deletion, or admin ACL
# commands. The only non-graph commands are connection-handshake commands.
cat >"$temporary_acl" <<EOF
user default off
user admin on >${FALKORDB_ADMIN_PASSWORD} ~* &* +@all
user reader_acme on >${FALKORDB_READER_ACME_PASSWORD} ~company_brain ~ops_topology ~agent_memory_acme ~probe_acme &* -@all +ping +hello +info +client|setinfo +client|setname +graph.ro_query
user reader_globex on >${FALKORDB_READER_GLOBEX_PASSWORD} ~company_brain ~ops_topology ~agent_memory_globex ~probe_globex &* -@all +ping +hello +info +client|setinfo +client|setname +graph.ro_query
user writer_acme on >${FALKORDB_WRITER_ACME_PASSWORD} ~ops_topology ~agent_memory_acme ~probe_acme &* -@all +ping +hello +info +client|setinfo +client|setname +graph.query +graph.ro_query
user writer_globex on >${FALKORDB_WRITER_GLOBEX_PASSWORD} ~ops_topology ~agent_memory_globex ~probe_globex &* -@all +ping +hello +info +client|setinfo +client|setname +graph.query +graph.ro_query
EOF

chmod 600 "$temporary_acl"
if [ "$(id -u)" = 0 ]; then
    chown 999:999 "$temporary_acl"
fi
mv -f "$temporary_acl" "$acl_file"

exec /var/lib/falkordb/bin/run.sh
