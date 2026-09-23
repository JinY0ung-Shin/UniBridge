# etcdctl / etcdutl toolbox on a shell-capable base.
#
# The upstream etcd image is distroless: it carries the etcd binaries and
# nothing else — no /bin/sh, no rm, no curl. etcd itself runs fine from it (see
# the `etcd` service in docker-compose.yml), but two jobs need a shell next to
# the CLIs:
#   * etcd/init-auth.sh   — the auth bootstrap the Bitnami image used to do
#                           (the `etcd-init` one-shot compose service).
#   * backup/lib/etcd.sh  — snapshot save / restore sibling containers.
# Both use this one image, so the etcd CLI version can never drift between the
# server, the bootstrap and the backups.
ARG ETCD_IMAGE=quay.io/coreos/etcd:v3.5.33

FROM ${ETCD_IMAGE} AS etcd

FROM alpine:3.21

# Optional mirror/proxy overrides, for parity with the other Dockerfiles in the
# repo. Nothing is installed here — busybox sh plus the two copied static Go
# binaries are all the scripts need — so a build that passes them still works.
ARG HTTP_PROXY=""
ARG HTTPS_PROXY=""
ARG NO_PROXY=""

COPY --from=etcd /usr/local/bin/etcdctl /usr/local/bin/etcdctl
COPY --from=etcd /usr/local/bin/etcdutl /usr/local/bin/etcdutl
COPY init-auth.sh /usr/local/bin/etcd-init-auth
RUN chmod 0755 /usr/local/bin/etcd-init-auth

ENV ETCDCTL_API=3

ENTRYPOINT ["/usr/local/bin/etcd-init-auth"]
