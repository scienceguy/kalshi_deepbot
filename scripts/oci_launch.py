#!/usr/bin/env python3
"""Launch a free-tier ARM VPS on Oracle Cloud and print its public IP.

Why the SDK and not the CLI: `pip install oci-cli` fails building crc32c on
termux, while the pure-python `oci` SDK installs fine.

Idempotent: reuses the VCN/subnet/instance if they already exist, so it is safe
to run in a retry loop until ARM capacity appears ("Out of host capacity").

Usage:
  python3 scripts/oci_launch.py              # ensure network, launch, print IP
  python3 scripts/oci_launch.py --status     # just show instances + IPs
  python3 scripts/oci_launch.py --ssh-key ~/.ssh/oci_ed25519.pub
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
import time

import oci

COMPARTMENT = os.environ.get(
    "OCI_COMPARTMENT",
    "ocid1.tenancy.oc1..aaaaaaaahmhpvfsnths4nwlbgimnaifkkidpgngq6og7out6brf7nexbvmra",
)
SHAPE = "VM.Standard.A1.Flex"
OCPUS, MEM_GB = 4, 24
VCN_NAME = "superbot-vcn"
INSTANCE_NAME = "superbot-trader"
TAG = "superbot"

USER_DATA = """#cloud-config
package_update: true
packages: [python3, python3-pip, python3-venv, git, tmux, rsync, curl, jq]
runcmd:
  - [ sh, -c, "iptables -I INPUT -p tcp --dport 22 -j ACCEPT || true" ]
  - [ sh, -c, "netfilter-persistent save || true" ]
  - [ sh, -c, "mkdir -p /opt/superbot && chown ubuntu:ubuntu /opt/superbot" ]
"""


def _cfg(args) -> dict:
    c = oci.config.from_file(os.path.expanduser("~/.oci/config"), "DEFAULT")
    c["region"] = c.get("region", "us-phoenix-1")
    return c


def _find(coll, pred):
    for item in coll:
        if pred(item):
            return item
    return None


def ensure_network(net, cid) -> str:
    """Ensure VCN + internet gateway + route + security list + public subnet."""
    vcns = net.list_vcns(cid, display_name=VCN_NAME).data
    vcn = vcns[0] if vcns else net.create_vcn(
        oci.core.models.CreateVcnDetails(
            compartment_id=cid, display_name=VCN_NAME,
            cidr_block="10.0.0.0/16", dns_label="superbot",
        )
    ).data
    print(f"vcn {vcn.id}", flush=True)
    vcn_id = vcn.id

    gws = net.list_internet_gateways(cid, vcn_id=vcn_id).data
    gw = gws[0] if gws else net.create_internet_gateway(
        oci.core.models.CreateInternetGatewayDetails(
            compartment_id=cid, vcn_id=vcn_id, is_enabled=True,
            display_name="superbot-igw",
        )
    ).data
    print(f"igw {gw.id}", flush=True)

    rts = net.list_route_tables(cid, vcn_id=vcn_id).data
    rt = rts[0]
    if not rt.route_rules:
        net.update_route_table(rt.id, oci.core.models.UpdateRouteTableDetails(
            route_rules=[oci.core.models.RouteRule(
                destination="0.0.0.0/0", destination_type="CIDR_BLOCK",
                network_entity_id=gw.id)]))
        print("route rule added", flush=True)

    sls = net.list_security_lists(cid, vcn_id=vcn_id).data
    sl = sls[0]
    want = [oci.core.models.IngressSecurityRule(
        protocol="6", source="0.0.0.0/0",
        tcp_options=oci.core.models.TcpOptions(
            destination_port_range=oci.core.models.PortRange(min=22, max=22)))]
    if not sl.ingress_security_rules:
        net.update_security_list(sl.id, oci.core.models.UpdateSecurityListDetails(
            ingress_security_rules=want,
            egress_security_rules=sl.egress_security_rules or [
                oci.core.models.EgressSecurityRule(protocol="all", destination="0.0.0.0/0")]))
        print("ssh ingress added", flush=True)

    subs = net.list_subnets(cid, vcn_id=vcn_id).data
    if subs:
        return subs[0].id
    sub = net.create_subnet(oci.core.models.CreateSubnetDetails(
        compartment_id=cid, vcn_id=vcn_id, display_name="superbot-subnet",
        cidr_block="10.0.0.0/24", dns_label="sb",
        route_table_id=rt.id, security_list_ids=[sl.id],
    )).data
    print(f"subnet {sub.id}", flush=True)
    return sub.id


def ubuntu_image(compute, cid) -> str:
    imgs = compute.list_images(
        cid, operating_system="Canonical Ubuntu",
        operating_system_version="24.04", shape=SHAPE,
        sort_by="TIMECREATED", sort_order="DESC").data
    if not imgs:
        raise SystemExit("no Ubuntu 24.04 image for VM.Standard.A1.Flex")
    return imgs[0].id


def existing(compute, cid):
    return [i for i in compute.list_instances(cid).data
            if i.display_name == INSTANCE_NAME and i.lifecycle_state != "TERMINATED"]


def public_ip(compute, net, cid, inst_id) -> str | None:
    atts = compute.list_vnic_attachments(cid, instance_id=inst_id).data
    for a in atts:
        if a.lifecycle_state != "ATTACHED":
            continue
        v = net.get_vnic(a.vnic_id).data
        if v.public_ip:
            return v.public_ip
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--ssh-key", default=os.path.expanduser("~/.ssh/oci_ed25519.pub"))
    ap.add_argument("--tries", type=int, default=1, help="launch attempts per AD")
    a = ap.parse_args()

    cfg = _cfg(a)
    cid = COMPARTMENT
    compute = oci.core.ComputeClient(cfg)
    net = oci.core.VirtualNetworkClient(cfg)

    if a.status:
        for i in existing(compute, cid):
            print(f"{i.id} {i.lifecycle_state} {i.shape} "
                  f"{getattr(i.shape_config, 'ocpus', '?')}ocpu "
                  f"ip={public_ip(compute, net, cid, i.id)}")
        return 0

    insts = existing(compute, cid)
    if insts:
        i = insts[0]
        print(f"exists {i.id} {i.lifecycle_state} ip={public_ip(compute, net, cid, i.id)}")
        return 0

    key = ""
    if os.path.exists(a.ssh_key):
        key = open(a.ssh_key).read().strip()
    else:
        print(f"WARN: no ssh public key at {a.ssh_key}; console access only", flush=True)

    subnet_id = ensure_network(net, cid)
    image_id = ubuntu_image(compute, cid)
    ads = oci.identity.IdentityClient(cfg).list_availability_domains(cid).data
    print(f"image {image_id}\nads {[ad.name for ad in ads]}", flush=True)

    last = None
    for attempt in range(a.tries):
        for ad in ads:
            try:
                inst = compute.launch_instance(oci.core.models.LaunchInstanceDetails(
                    compartment_id=cid, availability_domain=ad.name,
                    display_name=INSTANCE_NAME, shape=SHAPE,
                    shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                        ocpus=OCPUS, memory_in_gbs=MEM_GB),
                    source_details=oci.core.models.InstanceSourceViaImageDetails(
                        image_id=image_id, boot_volume_size_in_gbs=100),
                    create_vnic_details=oci.core.models.CreateVnicDetails(
                        subnet_id=subnet_id, assign_public_ip=True),
                    metadata={"ssh_authorized_keys": key,
                              "user_data": base64.b64encode(USER_DATA.encode()).decode()},
                    freeform_tags={"role": TAG},
                )).data
            except oci.exceptions.ServiceError as e:
                last = e
                print(f"ad {ad.name}: {e.status} {e.code} {e.message[:80]}", flush=True)
                continue
            print(f"launched {inst.id} in {ad.name}", flush=True)
            for _ in range(40):
                time.sleep(6)
                cur = compute.get_instance(inst.id).data
                ip = public_ip(compute, net, cid, inst.id)
                if cur.lifecycle_state == "RUNNING" and ip:
                    print(f"RUNNING ip={ip}")
                    return 0
            print(f"state {cur.lifecycle_state} ip={ip}")
            return 0
        if attempt + 1 < a.tries:
            time.sleep(30)
    print(f"launch failed: {last}", flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
