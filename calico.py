#!venv/bin/python
"""Calico..

Usage:
  calico master [--peer=<ADDRESS>...]
  calico launch --master=<ADDRESS> [--peer=<ADDRESS>...]
  calico run <IP> --master=<ADDRESS> [--group=<GROUP>] [--] <docker-options> ...
  calico status
  calico reset [--delete-images]
  calico version


Options:
 --peer=<ADDRESS>    The address of other compute node. Can be specified multiple times.
 --group=<GROUP>     The group to place the container in [default: DEFAULT]
 --master=<ADDRESS>  The address of the master node.
 <IP>                The IP to assign to the container.
"""
#   calico show me my containers and their groups and IPs.
#   calico ps
#   calico start
#   calico stop
#   calico attach
#   calico detach
#   calico expose
#   calico hide
#   calico version
# Some pretty important things that the current docker demo can't do:
#   Demonstrate container mobility
#   Expose services externally
#   Stop a service and clean everything up...

# TODO - Implement all these commands
# TODO - Bash completion
# TODO - Logging
# TODO -  Files should be written to a more reliable location, either relative to the binary or
# in a fixed location.

#Useful docker aliases
# alias docker_kill_all='sudo docker kill $(docker ps -q)'
# alias docker_rm_all='sudo docker rm -v `docker ps -a -q -f status=exited`'

from subprocess import call, check_output, check_call, CalledProcessError
from string import Template
import socket
import os
from docopt import docopt
import shlex
import sh
from sh import mkdir
from sh import docker
from sh import modprobe
from sh import rm
import requests
# from etcd.client import Client

docker_run = docker.bake("run", "-d", "--net=none")
mkdir_p = mkdir.bake('-p')
fig = sh.Command("./fig")
fig_master = fig.bake("-p calico", "-f", "master.yml")
fig_node = fig.bake("-p calico", "-f", "node.yml")

HOSTNAME = socket.gethostname()

import etcd

client = etcd.Client()

PLUGIN_TEMPLATE = Template("""[felix $name]
ip=$ip
host=$name

$peers
""")

ACL_TEMPLATE = Template("""
[global]
# Plugin address
PluginAddress = $ip
# Address to bind to - either "*" or an IPv4 address (or hostname)
#LocalAddress = *

[log]
# Log file path.
# Log file path. If LogFilePath is not set, acl_manager will not log to file.
LogFilePath = /var/log/calico/acl_manager.log

# Log severities for the Felix log and for syslog.
#   Valid levels: NONE (no logging), DEBUG, INFO, WARNING, ERROR, CRITICAL
LogSeverityFile   = DEBUG
#LogSeveritySys    = ERROR
LogSeverityScreen = CRITICAL
""")

def validate_arguments(arguments):
    # print(arguments)
    return True

def configure_bird(peers):
    # TODO Config shouldn't live here. Bird config should live with bird and another process in the
    # bird container (which should process felix.txt)
    neighbours = ""
    for peer in peers:
        neighbours += "  neighbor %s as 64511;\n" % socket.gethostbyname(peer)
    ip = socket.gethostbyname(HOSTNAME)

    bird_config = BIRD_TEMPLATE.substitute(ip=ip, neighbours=neighbours)
    with open('config/bird.conf', 'w') as f:
        f.write(bird_config)

def configure_felix(master_ip):
    felix_config = FELIX_TEMPLATE.substitute(ip=master_ip, hostname=HOSTNAME)
    with open('config/felix.cfg', 'w') as f:
        f.write(felix_config)

def configure_master_components(peers):
    peer_config = ""

    for peer in peers:
        peer_config += "[felix {name}]\nip={address}\nhost={name}\n\n".format(name=peer,
                                                                         address=peer)

    # We need our name and address and name and address for all peers
    plugin_config = PLUGIN_TEMPLATE.substitute(ip=HOSTNAME, name=HOSTNAME, peers=peer_config)
    with open('config/data/felix.txt', 'w') as f:
        f.write(plugin_config)

    # ACL manager just need address of plugin manager
    aclmanager_config = ACL_TEMPLATE.substitute(ip=HOSTNAME)
    with open('config/acl_manager.cfg', 'w') as f:
        f.write(aclmanager_config)

def create_dirs():
    mkdir_p("config/data")
    mkdir_p("/var/log/calico")

import sys
def process_output(line):
    sys.stdout.write(line)

def launch(master, peers):
    create_dirs()
    modprobe("ip6_tables")
    modprobe("xt_set")

    # configure_bird(peers)
    # configure_felix(master)
    # p = fig_node("up", "-d", _err=process_output, _out=process_output)
    # p.wait()
    # Assume that the image is already built - called calico_node
    docker("run", "-d", "calico/node")
    # TODO pass in the master as an env var?
    # I don't want to calico to run until it has the


def status():
    print(docker("ps"))

def run(ip, group, master, docker_options):
    cid = docker_run(shlex.split(docker_options)).strip()

    #Important to print this, since that's what docker run does when running a detached container.
    print cid

    cpid = docker("inspect", "-f", "'{{.State.Pid}}'", cid).strip()

    # TODO - need to handle containers exiting straight away...
    iface = "tap" + cid[:11]
    iface_tmp = "tap" + "%s-" % cid[:10]

    # Provision the networking
    mkdir_p("/var/run/netns")
    check_call("ln -s /proc/%s/ns/net /var/run/netns/%s" % (cpid, cpid), shell=True)

    # Create the veth pair and move one end into container as eth0 :
    check_call("ip link add %s type veth peer name %s" % (iface, iface_tmp), shell=True)
    check_call("ip link set %s up" % iface, shell=True)
    check_call("ip link set %s netns %s" % (iface_tmp, cpid), shell=True)
    check_call("ip netns exec %s ip link set dev %s name eth0" % (cpid, iface_tmp), shell=True)
    check_call("ip netns exec %s ip link set eth0 up" % cpid, shell=True)

    # Add an IP address to that thing :
    check_call("ip netns exec %s ip addr add %s/32 dev eth0" % (cpid, ip), shell=True)
    check_call("ip netns exec %s ip route add default dev eth0" % cpid, shell=True)

    # Get the MAC address.
    mac = check_output("ip netns exec %s ip link show eth0 | grep ether | awk '{print $2}'" % cpid, shell=True).strip()
    name = ip.replace('.', '_')
    base_config = """
[endpoint %s]
id=%s
ip=%s
mac=%s
host=%s
group=%s
""" % (name, cid, ip, mac, HOSTNAME, group)

    # Send the file to master
    # headers = {'content-type': 'application/json'}
    # r = requests.post("http://{host}:5000/upload/{filename}.txt".format(host=master,
    #                                                                     filename=name),
    #                   data=base_config, headers=headers)
    client.write('/calico/endpoints/%s' % name, base_config)


def reset(delete_images):
    print "Killing and removing Calico containers"
    fig_master("kill")
    fig_node("kill")

    fig_master("rm", "--force")
    fig_node("rm", "--force")

    print "Deleting all config"
    rm("-rf", "config")

    if (delete_images):
        print "Deleting images"
        docker("rmi", "calico_pluginep")
        docker("rmi", "calico_pluginnetwork")
        docker("rmi", "calico_bird")
        docker("rmi", "calico_felix")
        docker("rmi", "calico_aclmanager")

    try:
        interfaces_raw = check_output("ip link show | grep -Eo ' (tap(.*?)):' |grep -Eo '[^ :]+'", shell=True)
        print "Removing interfaces:\n%s" % interfaces_raw
        interfaces = interfaces_raw.splitlines()
        for interface in interfaces:
            call("ip link delete %s" % interface, shell=True)
    except CalledProcessError:
        print "No interfaces to clean up"


def version():
    #TODO call a fig -p calico build here too.
    print(docker("run", "--rm", "calico_felix", "apt-cache", "policy", "calico-felix"))


def master(peers):
    create_dirs()
    configure_master_components(peers)
    p = fig_master("up", "-d", _err=process_output, _out=process_output)
    p.wait()

if __name__ == '__main__':
    if os.geteuid() != 0:
        print "Calico must be run as root"
    else:
        arguments = docopt(__doc__)
        if validate_arguments(arguments):
            if arguments["master"]:
                master(arguments["--peer"])
            if arguments["launch"]:
                launch(arguments["--master"], arguments["--peer"])
            if arguments["run"]:
                run(arguments['<IP>'],
                    arguments['--group'],
                    arguments['--master'],
                    ' '.join(arguments['<docker-options>']))
            if arguments["status"]:
                status(arguments["--master"])
            if arguments["reset"]:
                reset(arguments["--delete-images"])
            if arguments["version"]:
                version()
        else:
            print "Not yet"
