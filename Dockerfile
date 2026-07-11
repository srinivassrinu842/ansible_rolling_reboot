FROM ubuntu:22.04

WORKDIR /data

# Copy the tar file into the image
COPY openshift-maintenance-1.0.0.tar.gz /data/openshift-maintenance-1.0.0.tar.gz
