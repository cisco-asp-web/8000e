# 8000e

# meta-bbf

### pyvxr topology and deployment

Deployment guide and topology access instructions for the Meta BBF simulation environment on SJC dCloud UCS server *`metabbf`* 198.18.128.101 

> [!NOTE]
> The server's netplan has been updated such that the primary interface *`eno1`* is attached to linux bridge *`br0`*, with ip address 198.18.128.101. This allows the 8000 emulator routers' management interfaces to be configured in the 198.18.128.x range and attached to *`br0`* for direct ssh access.


1. Deploy the topology (if not already running):
```
cd ~/meta-bbf/6-node-clos/

vxr.py start 6-node-clos.yaml 
```

The ovxr instances take about 10 minutes to boot
Successful deployment should end with console output looking something like:

```
18:19:19 INFO l01:applying XR config
18:19:19 INFO l02:applying XR config
18:19:19 INFO l03:applying XR config
18:19:19 INFO l04:applying XR config
18:19:19 INFO s01:applying XR config
18:19:19 INFO s02:applying XR config
18:19:22 INFO Sim up
```

2. ssh to routers - user/pw is *`cisco/cisco123`*:
   
| Node           | Address         |
|:---------------|:----------------|
| Spine01 or s01 | 198.18.128.201  |
| Spine02 or s02 | 198.18.128.202  |
| Leaf01 or l01  | 198.18.128.203  |
| Leaf02 or l02  | 198.18.128.204  |
| Leaf03 or l03  | 198.18.128.205  |
| Leaf04 or l04  | 198.18.128.206  |

3. Run the *`plumbing.sh`* script
 
This script will move the routers' mgt interfaces off virbr0 and onto *`br0`* for external reachability. 
   
The script will also spin up the *`ceos`* containers and attach them to linux bridge instances where they can peer with 8223 nodes *`l01`* and *`l02`*

```
./plumbing.sh
```

4. Access and configure *`ceos`* nodes with docker exec:
```
docker exec -it ceos1 Cli 
docker exec -it ceos2 Cli 
```


## Future: Containerlab deployment...under construction...
### Create containerlab docker image

Requires: 
* 8000 EFT emulator package and scripts
* 8223-64EF-M iso image
* 8223-64EF-M tar file

1. Extract 8000 EFT
```
tar -xvf 8000-emulator-eft17.0.tar
```

2. cd into 8000 EFT directory and execute ovxr-docker script
```
cd 8000-eft17.0 
```
```
python3 scripts/ovxr-docker/create_single_docker.py \
--iso-tar /home/cisco/images/8223-iso.tar \
--image-tar /home/cisco/images/8000-emulator-8223-64ef-m.tar \
--platform 8223-64EF-M \
--docker-name 8223-64EF-M:latest
```

