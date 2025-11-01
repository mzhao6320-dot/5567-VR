# Viewstamped Replication (VR) Simulation

This project demonstrates the **Viewstamped Replication (VR)** protocol, including its three major sub-protocols:

1. **Normal Operation Protocol**
2. **View Change Protocol**
3. **Replica Recovery Protocol**

Each node runs independently as a replica (primary or backup).  
The system ensures consistent replication and fault tolerance through view changes and recovery.

---

### Step 1: Open 4 Terminal Windows

#### Terminal 1 - Start Node 0
```bash
python vr_node.py --id 0 --port 6000 --peer_ports 6000,6001,6002,6003
```
You will see:
``` bath
Available commands:
 status - Check the status
 list - Show current primary and replica nodes
 data - View the submitted data (log and state)
 client <acct> <op> <amt> - Submit client request to local node (primary recommended)
 cancommit vote yes/no <op_index> - Vote for a PREPARE (sends VOTE to primary)
 do-view-change vote yes/no - Reply to a START-VIEW-CHANGE proposer
 start-view-change - Broadcast START-VIEW-CHANGE to cluster
 crash - Simulated crash
 recover - Recover from crash (sends RECOVERY request)
 quit - Exit node and clear its log file

[node0 P view=0] >>>
```

#### list
You will see:
``` bath
[node0 P view=0] >>> list
============================================================
[node 0] ROLE=PRIMARY | view=0 | crashed=False
------------------------------------------------------------
Current Primary : node0
Replica Nodes   : node1, node2, node3
------------------------------------------------------------
Local Role Map  : node0: PRIMARY, node1: BACKUP, node2: BACKUP, node3: BACKUP
============================================================
```

#### Terminal 2 - Start Node 1
``` bath
python vr_node.py --id 1 --port 6001 --peer_ports 6000,6001,6002,6003
```

#### Terminal 3 - Start Node 2
``` bath
python vr_node.py --id 2 --port 6002 --peer_ports 6000,6001,6002,6003
```

#### Terminal 4 - Start Node 3
``` bath
python vr_node.py --id 3 --port 6003 --peer_ports 6000,6001,6002,6003
```

All nodes will show:
``` bath
[nodeX B view=0] >>>
```

## 1.Normal Operation Protocol
### Step 2: Primary handles client requests
#### On Node 0 (primary):
##### The op command description >> client XXX deposit/withdraw BBB
##### Explanation: XXX represents the account name, and BBB represents a customizable numeric value.
client alice deposit 50
```

You will see:
``` bath
[primary 0] PREPARE op 1 request={'account': 'alice', 'operation': 'deposit', 'amount': '50'}
```

Each backup (1, 2, 3) receives:
``` bath
[node X] Received PREPARE for op 1, waiting for manual 'cancommit vote yes/no' to reply
```

### Step 3: Backups vote YES
#### On Node 1, Node 2, Node 3:
##### cancommit vote yes/no id （id denotes the voted transaction op id, such as 1, 2, 3, 4, etc.）
##### The total number of nodes is $2f + 1$. Once other $f$ votes of “yes” are received, the operation can be committed.
##### For example, in a system with 5 nodes (1 primary node and 4 replica nodes), receiving 2 “vote yes” messages from replica nodes is sufficient to commit the operation.
``` bath
cancommit vote yes 1
```

Then primary collects majority and commits:
``` bath
[primary 0] Received vote YES from node1 for op 1
[primary 0] Received vote YES from node2 for op 1
[primary 0] Majority YES for op 1 -> broadcasting COMMIT
[node 0] Applying COMMIT locally for op 1
[node 0] State now: {'alice': 50}
```

All replicas apply:
``` bath
[node X] Applying COMMIT op 1
[node X] State now: {'alice': 50}
```

Verify on any node:
``` bath
data
```

Output:
``` bath
LOG: [
  { "op_index": 1, "view": 0, "state": "COMMITTED",
    "request": {"account":"alice","operation":"deposit","amount":"50"} }
]
STATE: {'alice': 50}
```

## 2.View Change Protocol
### Step 4: Simulate primary crash
#### On Node 0:
``` bath
crash
```

Output:
``` bath
[node 0] Simulated CRASH. Node will not process messages until 'recover'
```

### Step 5: Start view change and elect a new primary (The view change process follows an ascending order. For example, if a crash occurs when the primary node’s id is 0, the next primary node will have id 1.)
#### On Node 1 (backup):
``` bath
start-view-change
```

You will see:
``` bath
[node 1] Broadcasted START-VIEW-CHANGE for view 1
```

Backup (2, 3) receives:
``` bath
[node X] Received START-VIEW-CHANGE for view 1 from 1
[node X] Please manually issue 'do-view-change vote yes/no' to send DO-VIEW-CHANGE to proposer 1
```

#### On Node 2/3 (backup):
``` bath
do-view-change vote yes
```

On node 1, you will see:
``` bath
[node 1] Broadcasted START-VIEW-CHANGE for view 1
[node1 B view=0] >>> [primary 1] Collected DO-VIEW-CHANGE from 2; collected 1
[primary 1] Collected DO-VIEW-CHANGE from 3; collected 2
[primary 1] Broadcasting START-VIEW for view 1
```
On node 2/3, you will see:
``` bath
[node X] View installed: view=1, primary=1
```
现在 Node 1 成为新主（[node1 P view=1]）。

### Step 6: New primary handles client requests
#### On Node 1 (new primary):
``` bath
client alice withdraw 40
```

You will see:
``` bath
[primary 1] PREPARE op 2 request={'account': 'alice', 'operation': 'withdraw', 'amount': '40'}
```

#### On backups (0 is crashed now; operate on Node 2 & 3):
``` bath
cancommit vote yes 2
```

Primary commits after majority:
``` bath
[primary 1] Majority YES for op 2 -> broadcasting COMMIT
[node X] Applying COMMIT op 2
[node X] State now: {'alice': 10}
```

Verify on any alive node:
``` bath
data
```

Output:
``` bath
LOG: [
  { "op_index": 1, "view": 0, "state": "COMMITTED",
    "request": {"account":"alice","operation":"deposit","amount":"50"} },
  { "op_index": 2, "view": 1, "state": "COMMITTED",
    "request": {"account":"alice","operation":"withdraw","amount":"40"} }
]
STATE: {'alice': 10}
```

## 3.Replica Recovery Protocol
### Step 7: Recover the crashed replica and synchronize
#### On Node 0 (previous primary):
``` bath
recover
```
You will see:
``` bath
[node 0] Recovering... Sending RECOVERY request to primary 0
[node 0] Received RECOVERY_REPLY from 1
[node0 P view=0] >>> [node 0] Rebuilding state from log ...
[node 0] REPLAY op1 view=0 deposit 50 -> alice: 0 -> 50
[node 0] REPLAY op2 view=1 withdraw 40 -> alice: 50 -> 10
[node 0] Rebuild done. commit_index=2, state={'alice': 10}
[node 0] Received RECOVERY_REPLY from 3
[node 0] Received RECOVERY_REPLY from 2
```

Verify on Node 0:
``` bath
data
```

Output:
``` bath
LOG: [
  { "op_index": 1, "view": 0, "state": "COMMITTED",
    "request": {"account":"alice","operation":"deposit","amount":"50"} },
  { "op_index": 2, "view": 1, "state": "COMMITTED",
    "request": {"account":"alice","operation":"withdraw","amount":"40"} }
]
STATE: {'alice': 10}
[node0 B view=1] >>>
```


## Shutdown:
Enter the following command on all terminals:
``` bath
quit
```

## Commands Reference
``` bath
status                       # Check node role & current view
list                         # Show current primary and replica nodes
data                         # Print local log & state
client <acct> <op> <amt>     # Submit a client request (e.g., client alice deposit 50)
cancommit vote yes/no <id>   # Vote for PREPARE <op_index>
start-view-change            # Initiate a new view change
do-view-change vote yes      # Approve a new primary (if required by your code)
crash                        # Simulate a crash (stop processing messages)
recover                      # Recover and synchronize from peers
quit                         # Exit node and clear its log file
```





