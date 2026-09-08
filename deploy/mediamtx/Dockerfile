ARG BUSYBOX_IMAGE=busybox:1.37.0-uclibc@sha256:8d7b1636e974e0adfd8d945955fca609304f0a56c18799dfd032d6e661382d84
ARG MEDIAMTX_IMAGE=bluenviron/mediamtx@sha256:1b029d11049be75630e9b73bb0d5f47b08a7db4eaee89a80bf8f53bc40e56414

FROM ${BUSYBOX_IMAGE} AS healthcheck_tools

FROM ${MEDIAMTX_IMAGE}
COPY --from=healthcheck_tools /bin/busybox /bin/busybox
COPY --from=healthcheck_tools /bin/busybox /bin/sh
