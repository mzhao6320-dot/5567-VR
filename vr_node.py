# python3
"""
vr_node.py

Interactive socket-based Viewstamped Replication (VR) simulator for N nodes (0..N-1).
Each node runs this program in its own terminal/process.

Usage examples:
  python vr_node.py --id 0 --port 5000 --peer_ports 5000,5001,5002,5003
  python vr_node.py --id 1 --port 5001 --peer_ports 5000,5001,5002,5003
  # or any size:
  python vr_node.py --id 0 --port 6000 --peer_ports 6000,6001,6002

Interactive commands available at each node:
  status                          - Check local status
  list                            - Show current primary and replica nodes
  data                            - View persisted log & state
  client <acct> <op> <amt>        - Submit client request (primary only recommended)
  cancommit vote yes/no <op_idx>  - Send PREPARE vote (PREPARE-OK / PREPARE-NO) to primary
  do-view-change vote yes/no      - Send DO-VIEW-CHANGE response (used after START-VIEW-CHANGE)
  start-view-change               - Propose a view change (START-VIEW-CHANGE)
  ack commit/abort <op_idx>       - Confirm COMMIT/ABORT (optional)
  crash                           - Simulate crash (node stops processing messages)
  recover                         - Recover from crash and run recovery (RECOVERY)
  fail <p>                        - Set outgoing message drop probability (0.0 - 1.0)
  quit                            - Exit program (and clear the persisted log file)
"""

import socket, threading, json, argparse, time, os, random
from typing import List, Tuple

DEFAULT_BASE_PORT = 5000
LOG_DIR = "."
RECV_BUF = 8192

# === Message helpers ===
def send_json(sock: socket.socket, addr: Tuple[str,int], obj: dict, drop_p: float):
    """Send JSON obj over UDP, may drop with probability drop_p (simulates network loss)."""
    if random.random() < drop_p:
        print(f"[network] DROPPED outgoing {obj.get('type')} -> {addr} (p={drop_p:.2f})")
        return
    data = json.dumps(obj).encode('utf-8')
    try:
        sock.sendto(data, addr)
    except Exception as e:
        print("[send_json] send failed:", e)

def recv_json(data: bytes):
    return json.loads(data.decode('utf-8', errors='ignore'))

# === Persistent Log ===
class PersistentLog:
    def __init__(self, node_id:int):
        self.path = os.path.join(LOG_DIR, f"node{node_id}_log.json")
        self.lock = threading.Lock()
        self.entries = []   # list of dicts: {op_index, view, state, request}
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    self.entries = json.load(f)
            except Exception:
                self.entries = []
        else:
            self.entries = []

    def append(self, rec: dict):
        with self.lock:
            self.entries.append(rec)
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.entries, f)

    def all(self):
        with self.lock:
            return list(self.entries)

    def set_state(self, op_index:int, new_state:str):
        with self.lock:
            for i,rec in enumerate(self.entries):
                if rec.get('op_index') == op_index:
                    self.entries[i]['state'] = new_state
            with open(self.path, 'w', encoding='utf-8') as f:
                json.dump(self.entries, f)

    def last_index(self):
        with self.lock:
            if not self.entries:
                return 0
            return max(rec['op_index'] for rec in self.entries)

# === VR Node Implementation ===
class VRNode:
    def __init__(self, node_id:int, port:int, peer_ports: List[int]):
        self.node_id = node_id
        self.port = port
        self.peer_ports = peer_ports  # index -> port for node id
        self.peers = [("127.0.0.1", p) for p in self.peer_ports]

        self.n_nodes = len(peer_ports)             # Dynamic number of nodes
        self.majority = self.n_nodes // 2 + 1      # Majority threshold

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", self.port))

        # VR state
        self.view = 0
        self.primary = self.view % self.n_nodes
        self.is_primary = (self.primary == self.node_id)
        self.op_counter = 0
        self.commit_index = 0
        self.log = PersistentLog(node_id)

        # application state: accounts map
        self.state = {}  # account -> int

        # operational flags
        self.crashed = False
        self.drop_prob = 0.0  # outgoing drop prob for simulating failures

        # protocol temporary structures
        self.pending_votes = {}        # op_index -> set(node_ids) votes YES
        self.pending_votes_no = {}     # op_index -> set(node_ids) votes NO (for tracking)
        self.waiting_prepare_from = {} # op_index -> entry

        # view change
        self.pending_view_change_msgs = []   # collected DO-VIEW-CHANGE (YES) for current vc
        self.view_change_completed = False   # Prevent multiple triggering of START-VIEW
        self.vc_new_view = None              # The target view number of the current round View-change
        self.vc_candidate_primary = None     # The candidate primary of this round view (new_view % N)
        self.pending_svc_new_view = None     # The new_view to be replied to after receiving the START-VIEW-CHANGE

        self.lock = threading.Lock()
        # For manual flows: when a START-VIEW-CHANGE arrives, we store whom to send DO-VIEW-CHANGE
        self.received_start_view_change_from = None  # What is actually stored is the node_id of the "candidate master"

        # heartbeat monitoring (optional)
        self.last_primary_heartbeat = time.time()
        self.heartbeat_interval = 1.0
        self.election_timeout = 5.0  # not enforced automatically (manual demo)

        # commit/apply bookkeeping
        self.committed_ops = set()
        self.applied_ops = set()  # The applied op_index

        # recovery helpers
        self.recovery_best = None
        self.recovery_completed = False

    # === Networking ===
    def send_to(self, node_id:int, msg:dict):
        addr = self.peers[node_id]
        send_json(self.sock, addr, msg, self.drop_prob)

    def broadcast(self, msg:dict):
        for nid in range(self.n_nodes):
            if nid == self.node_id:
                continue
            self.send_to(nid, msg)

    # === Message Handlers ===
    def handle_client_request(self, request:dict):
        """Primary only: receives a client request (account/op/amount) and starts PREPARE."""
        if not self.is_primary:
            print("[info] Not primary: forward request to primary at port", self.peer_ports[self.primary])
            msg = {'type':'CLIENT_FORWARD','from':self.node_id,'payload':request}
            self.send_to(self.primary, msg)
            return
        with self.lock:
            self.op_counter = max(self.op_counter, self.log.last_index()) + 1
            op_index = self.op_counter
            entry = {'op_index':op_index, 'view':self.view, 'state':'PREPARED', 'request':request}
            self.log.append(entry)
            self.pending_votes[op_index] = set()
            self.pending_votes_no[op_index] = set()
        print(f"[primary {self.node_id}] PREPARE op {op_index} request={request}")
        msg = {'type':'PREPARE','from':self.node_id,'view':self.view,'op_index':op_index,'request':request}
        self.broadcast(msg)

    def handle_prepare(self, msg:dict):
        """Backup receives PREPARE; manual vote via CLI."""
        if self.crashed:
            return
        op_index = msg['op_index']
        request = msg['request']
        view = msg['view']
        with self.lock:
            self.waiting_prepare_from[op_index] = {'from': msg['from'], 'view': view, 'request': request}
            rec = {'op_index':op_index,'view':view,'state':'PREPARED','request':request}
            self.log.append(rec)
        print(f"[node {self.node_id}] Received PREPARE for op {op_index}, waiting for manual 'cancommit vote yes/no' to reply")

    def handle_vote(self, msg:dict):
        """Primary receives VOTE from backups: msg contains 'vote'='YES'/'NO'."""
        if self.crashed or not self.is_primary:
            return
        op_index = msg['op_index']
        voter = msg['from']
        vote = msg['vote']
        with self.lock:
            if vote.upper() == 'YES':
                self.pending_votes.setdefault(op_index,set()).add(voter)
            else:
                self.pending_votes_no.setdefault(op_index,set()).add(voter)
            print(f"[primary {self.node_id}] Received vote {vote} from node{voter} for op {op_index}")

            # check for majority YES
            yes_count = len(self.pending_votes.get(op_index,set())) + 1  # +1 for primary itself
            if yes_count >= self.majority and op_index not in self.committed_ops:
                self.committed_ops.add(op_index)
                print(f"[primary {self.node_id}] Majority YES for op {op_index} -> broadcasting COMMIT")
                self.log.set_state(op_index,'COMMITTED')
                self.commit_index = max(self.commit_index, op_index)
                cm = {'type':'COMMIT','from':self.node_id,'view':self.view,'op_index':op_index,'request': msg.get('request')}
                self.broadcast(cm)

                if op_index not in self.applied_ops:
                    print(f"[node {self.node_id}] Applying COMMIT locally for op {op_index}")
                    self.apply_entry(op_index)
                    self.applied_ops.add(op_index)

            # if majority NO (enough NO to rule out majority YES), abort
            no_count = len(self.pending_votes_no.get(op_index,set()))
            if no_count > (self.n_nodes - self.majority):
                print(f"[primary {self.node_id}] Enough NO votes for op {op_index} -> broadcasting ABORT")
                self.log.set_state(op_index,'ABORTED')
                abort_msg = {'type':'ABORT','from':self.node_id,'view':self.view,'op_index':op_index}
                self.broadcast(abort_msg)

    def handle_commit(self, msg:dict):
        op_index = msg['op_index']
        if op_index in self.applied_ops:
            return  # Idempotence
        request = msg.get('request')
        self.log.set_state(op_index, 'COMMITTED')
        self.commit_index = max(self.commit_index, op_index)
        print(f"[node {self.node_id}] Applying COMMIT op {op_index}")
        self.apply_entry(op_index)
        self.applied_ops.add(op_index)

    def handle_abort(self, msg:dict):
        op_index = msg['op_index']
        self.log.set_state(op_index, 'ABORTED')
        print(f"[node {self.node_id}] ABORT op {op_index} recorded")

    def apply_entry(self, op_index:int):
        for rec in self.log.all():
            if rec['op_index'] == op_index:
                req = rec['request']
                acct = req['account']
                amt = int(req['amount'])
                op = req['operation']
                if op == 'deposit':
                    self.state[acct] = self.state.get(acct,0) + amt
                elif op == 'withdraw':
                    self.state[acct] = self.state.get(acct,0) - amt
                print(f"[node {self.node_id}] State now: {self.state}")
                return

    # === View-Change & Recovery ===
    def handle_start_view_change(self, msg:dict):
        new_view = msg['new_view']
        proposer = msg['from']
        candidate_primary = new_view % self.n_nodes
        print(f"[node {self.node_id}] Received START-VIEW-CHANGE for view {new_view} from {proposer}")
        # Backup node: Records "To whom (candidate master) the DO-VIEW-CHANGE should be voted" and "to which view"
        self.received_start_view_change_from = candidate_primary
        self.pending_svc_new_view = new_view
        print(f"[node {self.node_id}] Please manually issue 'do-view-change vote yes/no' to send DO-VIEW-CHANGE to candidate primary {candidate_primary}")

    def handle_do_view_change(self, msg:dict):
        """
        Only the candidate primary for that new_view collects YES votes and, on majority, sends START-VIEW(new_view).
        """
        # Determine which round of view-change the ticket belongs to
        msg_new_view = msg.get('new_view', None)
        if msg_new_view is None:
            # Compatibility with old messages: If new_view is not carried, it is regarded as the next view
            msg_new_view = self.view + 1

        candidate_primary = msg_new_view % self.n_nodes
        if self.node_id != candidate_primary:
            return  # If you are not a candidate, no votes will be accepted

        if self.view_change_completed:
            return

        # Only YES tickets are accepted
        if msg.get('vote', 'NO').upper() != 'YES':
            return

        with self.lock:
            self.vc_new_view = msg_new_view
            self.vc_candidate_primary = candidate_primary
            self.pending_view_change_msgs.append(msg)

            # YES de-duplication count
            yes_from = {m['from'] for m in self.pending_view_change_msgs}
            yes_count = 1 + len(yes_from)  # +1 contains the candidate master itself
            print(f"[primary {self.node_id}] Collected DO-VIEW-CHANGE from {msg['from']}; collected_yes={yes_count}")

            if yes_count >= self.majority and not self.view_change_completed:
                # Select the best log (the longest) and issue the START-VIEW
                best = max(
                    (m.get('log', []) for m in self.pending_view_change_msgs + [{'log': self.log.all()}]),
                    key=lambda L: len(L)
                )
                new_view = self.vc_new_view
                start_msg = {
                    'type': 'START-VIEW',
                    'from': self.node_id,
                    'new_view': new_view,
                    'primary': new_view % self.n_nodes,
                    'log': best
                }
                print(f"[primary {self.node_id}] Broadcasting START-VIEW for view {new_view}")
                self.broadcast(start_msg)

                # Local installation
                self.log.entries = best
                with open(self.log.path, 'w', encoding='utf-8') as f:
                    json.dump(self.log.entries, f)
                self.view = new_view
                self.primary = start_msg['primary']
                self.is_primary = (self.primary == self.node_id)
                self.view_change_completed = True
                self.rebuild_state_from_log()

    def handle_start_view(self, msg:dict):
        new_view = msg['new_view']
        new_primary = msg['primary']
        new_log = msg.get('log', [])
        print(f"[node {self.node_id}] START-VIEW received: install view {new_view}, primary {new_primary}")
        with self.lock:
            self.log.entries = new_log
            with open(self.log.path, 'w', encoding='utf-8') as f:
                json.dump(self.log.entries, f)
            self.view = new_view
            self.primary = new_primary
            self.is_primary = (self.node_id == new_primary)
            # Clear the status of this round of vc (to prevent repeated triggering
            self.view_change_completed = True
            self.vc_new_view = None
            self.vc_candidate_primary = None
            self.pending_view_change_msgs = []
            self.pending_svc_new_view = None
        self.rebuild_state_from_log()
        print(f"[node {self.node_id}] View installed: view={self.view}, primary={self.primary}")

    def handle_recovery_request(self, msg:dict):
        reqor = msg['from']
        port = msg['reply_port']
        resp = {
            'type':'RECOVERY_REPLY','from':self.node_id,
            'view':self.view,'log': self.log.all(),'commit_index': self.commit_index
        }
        print(f"[node {self.node_id}] Sending RECOVERY_REPLY to {reqor} at port {port}")
        send_json(self.sock, ('127.0.0.1', port), resp, self.drop_prob)

    def rebuild_state_from_log(self):
        print(f"[node {self.node_id}] Rebuilding state from log ...")
        self.state = {}
        max_committed = 0

        for rec in sorted(self.log.all(), key=lambda r: r['op_index']):
            st = rec.get('state')
            if st != 'COMMITTED':
                continue

            req  = rec['request']
            acct = str(req.get('account', '')).strip()
            op   = str(req.get('operation', '')).strip().lower()
            try:
                amt = int(str(req.get('amount', '0')).strip())
            except Exception:
                print(f"[node {self.node_id}] WARN: bad amount in rec {rec}")
                continue

            before = self.state.get(acct, 0)
            if op == 'deposit':
                after = before + amt
            elif op == 'withdraw':
                after = before - amt
            else:
                print(f"[node {self.node_id}] WARN: unknown op '{op}' in rec {rec}")
                continue

            self.state[acct] = after
            max_committed = max(max_committed, rec['op_index'])
            print(f"[node {self.node_id}] REPLAY op{rec['op_index']} view={rec.get('view')} {op} {amt} -> {acct}: {before} -> {after}")

        self.commit_index = max_committed
        self.applied_ops = {rec['op_index'] for rec in self.log.all() if rec.get('state') == 'COMMITTED'}
        print(f"[node {self.node_id}] Rebuild done. commit_index={self.commit_index}, state={self.state}")

    def handle_recovery_reply(self, msg:dict):
        print(f"[node {self.node_id}] Received RECOVERY_REPLY from {msg.get('from')}")

        if self.recovery_completed:
            return

        incoming_view = msg.get('view', -1)
        incoming_log  = msg.get('log', [])
        incoming_ci   = msg.get('commit_index', 0)

        best = self.recovery_best
        def better(a, b):
            if b is None:
                return True
            av, al, ac = a
            bv, bl, bc = b
            if av != bv: return av > bv
            if al != bl: return al > bl
            return ac > bc

        cand = (incoming_view, len(incoming_log), incoming_ci)
        best_tuple = None if best is None else (best['view'], len(best['log']), best['commit_index'])

        if better(cand, best_tuple):
            self.recovery_best = {'view': incoming_view, 'log': list(incoming_log), 'commit_index': incoming_ci}
            with self.lock:
                self.log.entries = list(incoming_log)
                with open(self.log.path, 'w', encoding='utf-8') as f:
                    json.dump(self.log.entries, f)
                self.view = incoming_view
                self.primary = self.view % self.n_nodes
                self.is_primary = (self.primary == self.node_id)
                self.commit_index = incoming_ci
                self.applied_ops = set()  # After clearing, it is rebuilt by replay
                self.rebuild_state_from_log()
            self.recovery_completed = True

    # === CLI command handlers (manual actions) ===
    def cli_cancommit_vote(self, vote:str, op_index:int=None):
        if self.crashed:
            print("[local] Node is crashed; cannot send vote")
            return
        if op_index is None:
            print("Please specify op_index with 'cancommit vote yes <op_index>'")
            return
        msg = {
            'type':'VOTE','from':self.node_id,'to':self.primary,
            'op_index':op_index,'vote': vote.upper(),
            'request': self.waiting_prepare_from.get(op_index,{}).get('request')
        }
        self.send_to(self.primary, msg)
        print(f"[node {self.node_id}] Sent VOTE {vote} for op {op_index} to primary {self.primary}")

    def cli_do_view_change_vote(self, vote:str):
        if self.crashed:
            print("[local] Node crashed; cannot send DO-VIEW-CHANGE")
            return
        proposer = self.received_start_view_change_from
        if proposer is None:
            print("[local] No START-VIEW-CHANGE pending")
            return
        new_view = self.pending_svc_new_view if self.pending_svc_new_view is not None else (self.view + 1)
        msg = {
            'type':'DO-VIEW-CHANGE','from':self.node_id,'to':proposer,
            'vote': vote.upper(),'log': self.log.all(),
            'commit_index': self.commit_index,'new_view': new_view
        }
        self.send_to(proposer, msg)
        print(f"[node {self.node_id}] Sent DO-VIEW-CHANGE ({vote}) to {proposer} for view {new_view}")
        self.received_start_view_change_from = None
        self.pending_svc_new_view = None

    def cli_start_view_change(self):
        if self.crashed:
            print("[local] Node crashed; cannot start view change")
            return

        new_view = self.view + 1
        candidate_primary = new_view % self.n_nodes

        with self.lock:
            self.pending_view_change_msgs = []
            self.view_change_completed = False
            self.vc_new_view = new_view
            self.vc_candidate_primary = candidate_primary

        msg = {'type':'START-VIEW-CHANGE','from':self.node_id,'new_view': new_view}
        self.broadcast(msg)
        print(f"[node {self.node_id}] Broadcasted START-VIEW-CHANGE for view {new_view} (candidate primary = {candidate_primary})")

    def cli_ack(self, what:str, op_index:int=None):
        if what not in ('commit','abort'):
            print("Use 'ack commit <op_index>' or 'ack abort <op_index>'")
            return
        if op_index is None:
            print("Please include op_index")
            return
        msg = {'type':'ACK','from':self.node_id,'to':self.primary,'what': what.upper(),'op_index': op_index}
        self.send_to(self.primary, msg)
        print(f"[node {self.node_id}] Sent ACK {what} for op {op_index}")

    def cli_crash(self):
        self.crashed = True
        print(f"[node {self.node_id}] Simulated CRASH. Node will not process messages until 'recover'")

    def cli_recover(self):
        if not self.crashed:
            print("[local] Node not crashed")
            return
        self.crashed = False

        # Reset the temporary variables for restoring the session
        self.recovery_best = None
        self.recovery_completed = False

        print(f"[node {self.node_id}] Recovering... Sending RECOVERY request to peers")
        msg = {'type':'RECOVERY_REQUEST','from':self.node_id,'reply_port': self.port}
        for nid in range(self.n_nodes):
            if nid != self.node_id:
                self.send_to(nid, msg)

    def cli_set_fail(self, p:float):
        self.drop_prob = float(p)
        print(f"[node {self.node_id}] set outgoing drop probability to {self.drop_prob}")

    # === Network receive loop ===
    def receive_loop(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(RECV_BUF)
                if self.crashed:
                    continue
                try:
                    msg = recv_json(data)
                except Exception as e:
                    print("[recv] JSON parse error", e)
                    continue
                mtype = msg.get('type')

                if mtype == 'CLIENT_FORWARD':
                    if self.is_primary:
                        self.handle_client_request(msg.get('payload'))
                elif mtype == 'CLIENT_REQUEST':
                    if self.is_primary:
                        self.handle_client_request(msg.get('request'))
                    else:
                        self.send_to(self.primary, msg)
                elif mtype == 'PREPARE':
                    self.handle_prepare(msg)
                elif mtype == 'VOTE':
                    self.handle_vote(msg)
                elif mtype == 'COMMIT':
                    self.handle_commit(msg)
                elif mtype == 'ABORT':
                    self.handle_abort(msg)
                elif mtype == 'START-VIEW-CHANGE':
                    self.handle_start_view_change(msg)
                elif mtype == 'DO-VIEW-CHANGE':
                    self.handle_do_view_change(msg)
                elif mtype == 'START-VIEW':
                    self.handle_start_view(msg)
                elif mtype == 'RECOVERY_REQUEST':
                    self.handle_recovery_request(msg)
                elif mtype == 'RECOVERY_REPLY':
                    self.handle_recovery_reply(msg)
                elif mtype == 'HEARTBEAT':
                    # Optional: update last heartbeat time from primary
                    self.last_primary_heartbeat = time.time()
                elif mtype == 'ACK':
                    print(f"[node {self.node_id}] Received ACK: {msg}")
                else:
                    pass
            except Exception:
                time.sleep(0.05)

    # === CLI main loop ===
    def repl(self):
        help_text = """
Available commands:
 status - Check the status
 list - Show current primary and replica nodes
 data - View the submitted data (log and state)
 client <acct> <op> <amt> - Submit client request to local node (primary recommended)
 cancommit vote yes/no <op_index> - Vote for a PREPARE (sends VOTE to primary)
 do-view-change vote yes/no - Reply YES/NO to candidate primary for a START-VIEW-CHANGE
 start-view-change - Broadcast START-VIEW-CHANGE to cluster
 crash - Simulated crash
 recover - Recover from crash (sends RECOVERY request)
 quit - Exit node and clear its log file
"""
        print(help_text)
        while True:
            try:
                role = 'P' if self.is_primary else 'B'
                cmd = input(f"[node{self.node_id} {role} view={self.view}] >>> ").strip()
            except EOFError:
                break
            if not cmd:
                continue
            parts = cmd.split()
            if parts[0] == 'status':
                print(f"node {self.node_id} | view={self.view} | primary={self.primary} | is_primary={self.is_primary} | crashed={self.crashed}")
                print(f" commit_index={self.commit_index} op_counter={self.op_counter}")
            elif parts[0] == 'data':
                print("LOG:", json.dumps(self.log.all(), indent=2))
                print("STATE:", self.state)
            elif parts[0] == 'client':
                if len(parts) < 4:
                    print("Usage: client <acct> <deposit/withdraw> <amount>")
                    continue
                acct, op, amt = parts[1], parts[2], parts[3]
                request = {'account':acct,'operation':op,'amount':amt}
                msg = {'type':'CLIENT_REQUEST','from':self.node_id,'request': request}
                if self.is_primary:
                    self.handle_client_request(request)
                else:
                    self.send_to(self.primary, msg)
                    print(f"[node {self.node_id}] Forwarded client request to primary {self.primary}")
            elif parts[0] == 'cancommit' and len(parts) >= 4 and parts[1] == 'vote':
                vote = parts[2]
                try:
                    op_index = int(parts[3])
                except:
                    print("Usage: cancommit vote yes/no <op_index>")
                    continue
                self.cli_cancommit_vote(vote, op_index)
            elif parts[0] == 'do-view-change' and len(parts) >= 3 and parts[1] == 'vote':
                vote = parts[2]
                self.cli_do_view_change_vote(vote)
            elif parts[0] == 'start-view-change':
                self.cli_start_view_change()
            elif parts[0] == 'ack' and len(parts) >= 3:
                what = parts[1]
                try:
                    op_idx = int(parts[2])
                except:
                    print("Usage: ack commit/abort <op_index>")
                    continue
                self.cli_ack(what, op_idx)
            elif parts[0] == 'crash':
                self.cli_crash()
            elif parts[0] == 'recover':
                self.cli_recover()
            elif parts[0] == 'fail' and len(parts) >= 2:
                try:
                    p = float(parts[1])
                except:
                    print("fail <p> where p in [0.0,1.0]")
                    continue
                self.cli_set_fail(p)
            elif parts[0] == 'quit':
                self.cli_quit()
                break
            elif parts[0] == 'list':
                self.cli_list()
                continue
            else:
                print("Unknown command. Press Enter for help.")

    # === Runner ===
    def start(self):
        t_recv = threading.Thread(target=self.receive_loop, daemon=True)
        t_recv.start()
        t_hb = threading.Thread(target=self.heartbeat_loop, daemon=True)
        t_hb.start()
        self.repl()

    def heartbeat_loop(self):
        while True:
            time.sleep(self.heartbeat_interval)
            if self.crashed:
                continue
            if self.is_primary:
                msg = {'type':'HEARTBEAT','from':self.node_id,'view':self.view}
                self.broadcast(msg)
            else:
                # manual demo: we don't auto-start elections
                pass

    # === list / quit ===
    def cli_list(self):
        primary = self.view % self.n_nodes
        role = "PRIMARY" if self.is_primary else "BACKUP"
        print("============================================================")
        print(f"[node {self.node_id}] ROLE={role} | view={self.view} | crashed={self.crashed}")
        print("------------------------------------------------------------")
        print(f"Total Nodes     : {self.n_nodes}")
        print(f"Current Primary : node{primary}")
        replicas = [f"node{i}" + (" (me)" if i == self.node_id else "")
                    for i in range(self.n_nodes) if i != primary]
        print("Replica Nodes   : " + (", ".join(replicas) if replicas else "(none)"))
        all_roles = []
        for i in range(self.n_nodes):
            all_roles.append(f"node{i}: " + ("PRIMARY" if i == primary else "BACKUP"))
        print("Local Role Map  : " + ", ".join(all_roles))
        print("============================================================")

    def cli_quit(self):
        print(f"[node {self.node_id}] Quitting... clearing log file {self.log.path}")
        try:
            with open(self.log.path, 'w', encoding='utf-8') as f:
                f.write("[]")
            self.log.entries = []
            self.state = {}
            self.commit_index = 0
            self.applied_ops = set()
            print(f"[node {self.node_id}] Log file cleared successfully.")
        except Exception as e:
            print(f"[node {self.node_id}] Error clearing log: {e}")
        print(f"[node {self.node_id}] Exiting now.")
        os._exit(0)


# === Main entry point ===
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--id', type=int, required=True, help='node id 0..N-1')
    parser.add_argument('--port', type=int, required=True, help='listening port for this node')
    parser.add_argument('--peer_ports', type=str, default=None, help='comma-separated ports for nodes 0..N-1 (required)')
    args = parser.parse_args()

    if not args.peer_ports:
        raise SystemExit("--peer_ports is required (comma-separated list of ports for all nodes)")

    parts = args.peer_ports.split(',')
    try:
        peer_ports = [int(x) for x in parts]
    except:
        raise SystemExit("peer_ports must be comma-separated integers")

    n_nodes = len(peer_ports)
    if args.id < 0 or args.id >= n_nodes:
        raise SystemExit(f"--id must be in range 0..{n_nodes-1}")

    # override own port with provided --port
    peer_ports[args.id] = args.port

    # create node and start
    node = VRNode(node_id=args.id, port=args.port, peer_ports=peer_ports)
    node.start()
