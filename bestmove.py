#!/usr/bin/env python3
"""
bestmove.py - paste chess moves, get Stockfish's best move.

No extra packages needed (pure Python 3.8+).

Usage (Windows, in the folder with this file):
    python bestmove.py
    python bestmove.py "C:\\path\\to\\stockfish.exe"

It asks if you are White or Black. Then you only type your OPPONENT's moves;
it works out and prints YOUR move each time.

Commands:  new   = start a fresh game
           undo  = take back the last move
           fen <fen> = start from a position
           quit  = exit
Pasting a whole game again from move 1 also works (it restarts automatically).
"""
import os
import re
import subprocess
import sys
import threading
import queue

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
FILES = "abcdefgh"

class Board:
    """Minimal chess rules: legal moves, SAN in/out. Squares 0..63, a1=0."""

    def __init__(self, fen=START_FEN):
        self.set_fen(fen)

    def set_fen(self, fen):
        parts = fen.split()
        if len(parts) < 4:
            raise ValueError("bad FEN")
        self.sq = [None] * 64
        rows = parts[0].split("/")
        if len(rows) != 8:
            raise ValueError("bad FEN")
        for r, row in enumerate(rows):
            f = 0
            for ch in row:
                if ch.isdigit():
                    f += int(ch)
                else:
                    self.sq[(7 - r) * 8 + f] = ch
                    f += 1
        self.white = parts[1] == "w"
        self.castle = set(parts[2]) - {"-"}
        self.ep = None if parts[3] == "-" else self._sqi(parts[3])
        self.half = int(parts[4]) if len(parts) > 4 else 0
        self.full = int(parts[5]) if len(parts) > 5 else 1

    @staticmethod
    def _sqi(name):
        return FILES.index(name[0]) + 8 * (int(name[1]) - 1)

    @staticmethod
    def name(i):
        return FILES[i % 8] + str(i // 8 + 1)

    def copy(self):
        b = Board.__new__(Board)
        b.sq = self.sq[:]
        b.white = self.white
        b.castle = set(self.castle)
        b.ep = self.ep
        b.half = self.half
        b.full = self.full
        return b

    def is_mine(self, p):
        return p is not None and p.isupper() == self.white

    def attacked(self, target, by_white):
        tf, tr = target % 8, target // 8
        # pawns
        pr = tr - 1 if by_white else tr + 1
        for df in (-1, 1):
            f = tf + df
            if 0 <= f < 8 and 0 <= pr < 8:
                if self.sq[pr * 8 + f] == ("P" if by_white else "p"):
                    return True
        # knights
        n = "N" if by_white else "n"
        for df, dr in ((1, 2), (2, 1), (-1, 2), (-2, 1), (1, -2), (2, -1), (-1, -2), (-2, -1)):
            f, r = tf + df, tr + dr
            if 0 <= f < 8 and 0 <= r < 8 and self.sq[r * 8 + f] == n:
                return True
        # king
        k = "K" if by_white else "k"
        for df in (-1, 0, 1):
            for dr in (-1, 0, 1):
                if df or dr:
                    f, r = tf + df, tr + dr
                    if 0 <= f < 8 and 0 <= r < 8 and self.sq[r * 8 + f] == k:
                        return True
        # sliders
        rook = ("R", "Q") if by_white else ("r", "q")
        bish = ("B", "Q") if by_white else ("b", "q")
        for dirs, pieces in (((1, 0), (-1, 0), (0, 1), (0, -1)), rook), (((1, 1), (1, -1), (-1, 1), (-1, -1)), bish):
            for df, dr in dirs:
                f, r = tf + df, tr + dr
                while 0 <= f < 8 and 0 <= r < 8:
                    p = self.sq[r * 8 + f]
                    if p is not None:
                        if p in pieces:
                            return True
                        break
                    f += df
                    r += dr
        return False

    def king_sq(self, white):
        k = "K" if white else "k"
        return self.sq.index(k)

    def in_check(self, white=None):
        if white is None:
            white = self.white
        return self.attacked(self.king_sq(white), not white)

    def pseudo(self):
        moves = []
        for i, p in enumerate(self.sq):
            if not self.is_mine(p):
                continue
            f, r = i % 8, i // 8
            t = p.upper()
            if t == "P":
                d = 1 if self.white else -1
                start = 1 if self.white else 6
                last = 7 if self.white else 0
                r1 = r + d
                if 0 <= r1 < 8 and self.sq[r1 * 8 + f] is None:
                    self._pawn_add(moves, i, r1 * 8 + f, r1 == last)
                    if r == start and self.sq[(r + 2 * d) * 8 + f] is None:
                        moves.append((i, (r + 2 * d) * 8 + f, None))
                for df in (-1, 1):
                    nf = f + df
                    if 0 <= nf < 8 and 0 <= r1 < 8:
                        to = r1 * 8 + nf
                        tp = self.sq[to]
                        if (tp is not None and not self.is_mine(tp)) or to == self.ep:
                            self._pawn_add(moves, i, to, r1 == last)
            elif t in "NK":
                steps = ((1, 2), (2, 1), (-1, 2), (-2, 1), (1, -2), (2, -1), (-1, -2), (-2, -1)) if t == "N" else \
                    tuple((a, b) for a in (-1, 0, 1) for b in (-1, 0, 1) if a or b)
                for df, dr in steps:
                    nf, nr = f + df, r + dr
                    if 0 <= nf < 8 and 0 <= nr < 8:
                        to = nr * 8 + nf
                        if not self.is_mine(self.sq[to]):
                            moves.append((i, to, None))
                if t == "K":
                    self._castles(moves, i)
            else:
                dirs = []
                if t in "RQ":
                    dirs += [(1, 0), (-1, 0), (0, 1), (0, -1)]
                if t in "BQ":
                    dirs += [(1, 1), (1, -1), (-1, 1), (-1, -1)]
                for df, dr in dirs:
                    nf, nr = f + df, r + dr
                    while 0 <= nf < 8 and 0 <= nr < 8:
                        to = nr * 8 + nf
                        tp = self.sq[to]
                        if tp is None:
                            moves.append((i, to, None))
                        else:
                            if not self.is_mine(tp):
                                moves.append((i, to, None))
                            break
                        nf += df
                        nr += dr
        return moves

    @staticmethod
    def _pawn_add(moves, frm, to, promo):
        if promo:
            for pc in "qrbn":
                moves.append((frm, to, pc))
        else:
            moves.append((frm, to, None))

    def _castles(self, moves, i):
        w = self.white
        home = 4 if w else 60
        if i != home or self.in_check():
            return
        enemy = not w
        base = 0 if w else 56
        if ("K" if w else "k") in self.castle:
            if self.sq[base + 5] is None and self.sq[base + 6] is None and \
               self.sq[base + 7] == ("R" if w else "r") and \
               not self.attacked(base + 5, enemy) and not self.attacked(base + 6, enemy):
                moves.append((i, base + 6, None))
        if ("Q" if w else "q") in self.castle:
            if self.sq[base + 1] is None and self.sq[base + 2] is None and self.sq[base + 3] is None and \
               self.sq[base + 0] == ("R" if w else "r") and \
               not self.attacked(base + 3, enemy) and not self.attacked(base + 2, enemy):
                moves.append((i, base + 2, None))

    def legal(self):
        out = []
        for m in self.pseudo():
            b = self.copy()
            b._apply(m)
            if not b.in_check(self.white):
                out.append(m)
        return out

    def _apply(self, m):
        frm, to, promo = m
        p = self.sq[frm]
        cap = self.sq[to]
        t = p.upper()
        new_ep = None
        if t == "P" and to == self.ep and cap is None and frm % 8 != to % 8:
            self.sq[to + (-8 if self.white else 8)] = None
        if t == "P" and abs(to - frm) == 16:
            new_ep = (frm + to) // 2
        if t == "K" and abs(to - frm) == 2:
            base = frm - 4
            if to > frm:
                self.sq[base + 5], self.sq[base + 7] = self.sq[base + 7], None
            else:
                self.sq[base + 3], self.sq[base + 0] = self.sq[base + 0], None
        self.sq[to] = (promo.upper() if self.white else promo) if promo else p
        self.sq[frm] = None
        # castling rights
        if t == "K":
            self.castle -= set("KQ" if self.white else "kq")
        for s, c in ((0, "Q"), (7, "K"), (56, "q"), (63, "k")):
            if frm == s or to == s:
                self.castle.discard(c)
        self.half = 0 if (t == "P" or cap is not None) else self.half + 1
        if not self.white:
            self.full += 1
        self.white = not self.white
        self.ep = new_ep

    def push(self, m):
        self._apply(m)

    def uci(self, m):
        return self.name(m[0]) + self.name(m[1]) + (m[2] or "")

    def san(self, m):
        frm, to, promo = m
        p = self.sq[frm]
        t = p.upper()
        if t == "K" and abs(to - frm) == 2:
            s = "O-O" if to > frm else "O-O-O"
        else:
            capture = self.sq[to] is not None or (t == "P" and frm % 8 != to % 8)
            if t == "P":
                s = (FILES[frm % 8] + "x" if capture else "") + self.name(to)
                if promo:
                    s += "=" + promo.upper()
            else:
                s = t
                others = [o for o in self.legal() if o[1] == to and o[0] != frm and self.sq[o[0]] == p]
                if others:
                    if all(o[0] % 8 != frm % 8 for o in others):
                        s += FILES[frm % 8]
                    elif all(o[0] // 8 != frm // 8 for o in others):
                        s += str(frm // 8 + 1)
                    else:
                        s += self.name(frm)
                s += ("x" if capture else "") + self.name(to)
        b = self.copy()
        b.push(m)
        if b.in_check():
            s += "#" if not b.legal() else "+"
        return s

    def describe(self, m):
        """Plain English, e.g. 'Knight from b1 to c3 (captures pawn)'."""
        frm, to, promo = m
        names = {"P": "Pawn", "N": "Knight", "B": "Bishop", "R": "Rook", "Q": "Queen", "K": "King"}
        t = self.sq[frm].upper()
        if t == "K" and abs(to - frm) == 2:
            side = "kingside" if to > frm else "queenside"
            return "Castle %s: King %s to %s, Rook moves too" % (side, self.name(frm), self.name(to))
        s = "%s from %s to %s" % (names[t], self.name(frm), self.name(to))
        victim = self.sq[to]
        if victim is not None:
            s += " (captures %s)" % names[victim.upper()].lower()
        elif t == "P" and frm % 8 != to % 8:
            s += " (captures pawn en passant)"
        if promo:
            s += " (promotes to %s)" % names[promo.upper()].lower()
        return s

    def parse_san(self, text):
        try:
            return self._parse_san_core(text)
        except ValueError as first:
            t = text.strip()
            variants = []
            if re.match(r"[nrqkb][a-h]?[1-8]?x?[a-h][1-8]", t):   # nf6, bc4, nbd2, qxd5
                variants.append(t[0].upper() + t[1:])
            if t.lower().startswith("o-o"):
                variants.append(t.upper())
            for v in variants:
                try:
                    return self._parse_san_core(v)
                except ValueError:
                    pass
            raise first

    def _parse_san_core(self, text):
        clean = text.strip().replace("0", "O").rstrip("+#!?")
        clean = re.sub(r"e\.p\.$", "", clean)
        clean = clean.replace("\u2013", "-")
        legal = self.legal()
        if not clean:
            raise ValueError("empty move")
        # accept raw UCI too (e2e4, e7e8q)
        if re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", clean):
            for m in legal:
                if self.uci(m) == clean:
                    return m
        want = clean.replace("=", "")
        for m in legal:
            s = self.san(m).rstrip("+#").replace("=", "")
            if s == want:
                return m
        #lenient/missing disambiguation
        norm = lambda x: x.replace("x", "").replace("-", "")
        cands = [m for m in legal if norm(self.san(m).rstrip("+#").replace("=", "")) == norm(want)]
        if len(cands) == 1:
            return cands[0]
        #"xe5", "f3" (knight), "Nbd2" "bd2"
        mm = re.fullmatch(r"([a-h]?[1-8]?)(x?)([a-h][1-8])(?:=?([QRBNqrbn]))?", clean)
        if mm:
            dis, is_cap, dest, promo = mm.group(1), bool(mm.group(2)), self._sqi(mm.group(3)), mm.group(4)
            found = []
            for m in legal:
                if m[1] != dest:
                    continue
                if promo and (m[2] or "") != promo.lower():
                    continue
                if is_cap and self.sq[dest] is None and not (self.sq[m[0]].upper() == "P" and dest == self.ep):
                    continue
                fn = self.name(m[0])
                if dis and dis not in fn:
                    continue
                found.append(m)
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                opts = ", ".join(self.san(m).rstrip("+#") for m in found)
                raise ValueError("'%s' could be several moves (%s) - type the one that was played" % (text, opts))
        raise ValueError("illegal or unreadable move: " + text)


SAN_PIECE = r"(?:O-O-O|O-O|[KQRBN][a-h]?[1-8]?x?[a-h][1-8]|[a-h](?:x[a-h])?[1-8](?:=[QRBN])?)[+#]?"


def split_glued(tok):
    """'d4d5' -> ['d4','d5'], 'Nf3Nc6' -> ['Nf3','Nc6']. None if not glued moves."""
    if re.fullmatch(SAN_PIECE, tok):
        return None
    parts, pos = [], 0
    while pos < len(tok):
        m = re.compile(SAN_PIECE).match(tok, pos)
        if not m:
            return None
        parts.append(m.group(0))
        pos = m.end()
    return parts if len(parts) > 1 else None


def tokenize(text):
    text = re.sub(r"\{[^}]*\}", " ", text)          
    text = re.sub(r"\([^)]*\)", " ", text)        
    text = re.sub(r"\[[^\]]*\]", " ", text)       
    text = re.sub(r"\$\d+", " ", text)         
    pieces = re.split(r"(?<![A-Za-z0-9])(\d+)\s*(\.\.\.|\.)", text)
    segs = []
    if pieces[0].strip():
        segs.append((0, 0, pieces[0]))
    for i in range(1, len(pieces) - 2, 3):
        segs.append((int(pieces[i]), 1 if pieces[i + 1] == "..." else 0, pieces[i + 2]))
    keys = [(n, d) for n, d, _ in segs]
    if keys != sorted(keys):
        segs.sort(key=lambda x: (x[0], x[1]))
    filler = {"then", "and", "next", "->", "=>", ">", "-", "&"}
    toks = []
    for _, _, seg in segs:
        for t in seg.replace(",", " ").replace(";", " ").split():
            if t.lower() in filler or t in ("1-0", "0-1", "1/2-1/2", "*"):
                continue
            toks.append(t)
    return toks

class Engine:
    def __init__(self, path):
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                  cwd=os.path.dirname(os.path.abspath(path)) or None)
        self.q = queue.Queue()
        threading.Thread(target=self._reader, daemon=True).start()
        self.name = "unknown engine"
        self.send("uci")
        while True:
            line = self.q.get()
            if line is None:
                raise RuntimeError("engine exited - is this really a Stockfish .exe?")
            if line.startswith("id name"):
                self.name = line[8:].strip()
            if line.startswith("uciok"):
                break
        self.send("isready")
        self.wait_for("readyok")

    def _reader(self):
        for line in self.p.stdout:
            self.q.put(line.strip())
        self.q.put(None)

    def send(self, s):
        self.p.stdin.write(s + "\n")
        self.p.stdin.flush()

    def wait_for(self, prefix):
        while True:
            line = self.q.get()
            if line is None:
                raise RuntimeError("engine exited")
            if line.startswith(prefix):
                return line

    def set_option(self, name, value):
        self.send(f"setoption name {name} value {value}")

    def best(self, start_fen, ucimoves, movetime_ms, multipv=1):
        self.send("isready")
        self.wait_for("readyok")
        pos = "position fen " + start_fen
        if ucimoves:
            pos += " moves " + " ".join(ucimoves)
        self.send(pos)
        self.send(f"go movetime {movetime_ms}")
        infos = {}
        while True:
            line = self.q.get()
            if line is None:
                raise RuntimeError("engine exited")
            if line.startswith("info") and " pv " in line and " score " in line:
                m = re.search(r"multipv (\d+)", line)
                k = int(m.group(1)) if m else 1
                infos[k] = line
            if line.startswith("bestmove"):
                return line.split()[1], infos

    def close(self):
        try:
            self.send("quit")
            self.p.wait(timeout=2)
        except Exception:
            self.p.kill()


def format_score(info, white_to_move):
    m = re.search(r"score (cp|mate) (-?\d+)", info)
    if not m:
        return "?"
    kind, v = m.group(1), int(m.group(2))
    if kind == "mate":
        return ("mate in %d" % v) if v > 0 else ("getting mated in %d" % -v)
    return "%+.2f" % (v / 100.0)


def find_stockfish(arg):
    if arg and os.path.isfile(arg):
        return arg
    here = os.path.dirname(os.path.abspath(__file__))
    for root in (here, os.getcwd()):
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.lower().startswith("stockfish") and f.lower().endswith(".exe") or f == "stockfish":
                    full = os.path.join(dirpath, f)
                    if os.access(full, os.X_OK) or full.endswith(".exe"):
                        return full
    return None

_lines = queue.Queue()


def _stdin_reader():
    while True:
        line = sys.stdin.readline()
        if not line:
            _lines.put(None)
            return
        _lines.put(line.rstrip("\r\n"))


def read_block(quiet=0.35):
    """One move typed, or many lines pasted at once: everything that arrives
    within a fraction of a second is treated as ONE input."""
    print("> ", end="", flush=True)
    first = _lines.get()
    if first is None:
        return None
    lines = [first]
    while True:
        try:
            line = _lines.get(timeout=quiet)
        except queue.Empty:
            break
        if line is None:
            _lines.put(None)
            break
        lines.append(line)
    return "\n".join(lines)


def main():
    exe = find_stockfish(sys.argv[1] if len(sys.argv) > 1 else None)
    if not exe:
        print("Could not find Stockfish. Run:  python bestmove.py \"C:\\path\\to\\stockfish.exe\"")
        return 1
    print("Using engine:", exe)
    eng = Engine(exe)
    print("Engine reports:", eng.name)
    cores = os.cpu_count() or 2
    for name, value in (("Threads", cores),          
                        ("Hash", 1024),                
                        ("Skill Level", 20),           
                        ("UCI_LimitStrength", "false"),
                        ("Ponder", "false")):
        try:
            eng.set_option(name, value)
        except Exception:
            pass
    print("Settings: max strength, %d threads, 1024 MB hash, 1s per move. Type fast (0.5s) for quicker or deep (10s) for stronger" % cores)

    state = {"movetime": 1000, "start_fen": START_FEN, "board": Board(START_FEN), "history": [], "me_white": True}

    def rebuild():
        b = Board(state["start_fen"])
        for m in state["history"]:
            b.push(m)
        state["board"] = b

    def my_turn():
        return state["board"].white == state["me_white"]

    def think_and_play():
        """Engine picks the move for YOUR side and adds it to the game."""
        board = state["board"]
        side = "White" if board.white else "Black"
        print(f"  Thinking for {side}...", end="", flush=True)
        ucis = [Board.name(m[0]) + Board.name(m[1]) + (m[2] or "") for m in state["history"]]
        best, infos = eng.best(state["start_fen"], ucis, state["movetime"])
        print("\r" + " " * 40 + "\r", end="")
        bm = next((m for m in board.legal() if board.uci(m) == best), None)
        if not bm:
            print("  Engine said:", best)
            return
        score = format_score(infos.get(1, ""), board.white)
        san = board.san(bm)
        what = board.describe(bm)
        state["history"].append(bm)
        board.push(bm)
        if len(state["history"]) == 1:
            print("  (Start of the game - this is your opening move. If you already have moves, just paste them.)")
        print(f"  >>> PLAY: {what}")
        print(f"      notation: {san}   ({best})   eval {score}")
        if not board.legal():
            print("  Game over:", "checkmate!" if board.in_check() else "stalemate.")
        else:
            other = "White" if board.white else "Black"
            print(f"  Now type {other}'s reply.")

    def after_input():
        board = state["board"]
        if not board.legal():
            print("  Game over:", "checkmate." if board.in_check() else "stalemate.")
        elif my_turn():
            think_and_play()
        else:
            print("  Waiting for", "White" if board.white else "Black", "- type their move.")

    def pick_side(white):
        state.update(start_fen=START_FEN, history=[], me_white=white)
        state["board"] = Board(START_FEN)
        print("You are %s. New game." % ("White" if white else "Black"))
        after_input()

    print("\nbestmove.py - type the OPPONENT's moves, I tell you yours.")
    print("Commands: white, black, new, undo (undo 2 = take back 2), fen <fen>, fast / normal / deep, time <seconds>, quit")
    print("Paste 1 move or many at once. If you played something other than my suggestion, just type your move and then the reply.\n")
    ans = input("Are you playing White or Black? [W/b]: ").strip().lower()
    threading.Thread(target=_stdin_reader, daemon=True).start()
    pick_side(not ans.startswith("b"))

    while True:
        try:
            raw = read_block()
        except KeyboardInterrupt:
            break
        if raw is None:
            break
        text = raw.strip()
        if not text:
            continue
        low = text.lower()
        if low in ("quit", "exit", "q"):
            break
        if low == "white":
            pick_side(True)
            continue
        if low == "black":
            pick_side(False)
            continue
        if low == "new":
            pick_side(state["me_white"])
            continue
        if low == "undo" or (low.startswith("undo ") and low.split()[1].isdigit()):
            n = int(low.split()[1]) if " " in low else 1
            if state["history"]:
                for _ in range(min(n, len(state["history"]))):
                    state["history"].pop()
                rebuild()
                b = state["board"]
                print("  Took back %d move(s)." % n, "White" if b.white else "Black", "to move - type the move that was actually played.")
            else:
                print("  Nothing to undo.")
            continue
        if low.startswith("fen "):
            try:
                fen = text[4:].strip()
                state["board"] = Board(fen)
                state.update(start_fen=fen, history=[])
            except Exception as e:
                print("  Bad FEN:", e)
                continue
            after_input()
            continue
        if low in ("fast", "normal", "deep"):
            state["movetime"] = {"fast": 500, "normal": 1000, "deep": 10000}[low]
            print("  Speed '%s': %.1fs per move" % (low, state["movetime"] / 1000))
            continue
        if low.startswith("time "):
            try:
                state["movetime"] = int(float(low.split()[1]) * 1000)
                print("  Thinking time: %.1fs" % (state["movetime"] / 1000))
            except Exception:
                print("  Usage: time 5")
            continue

        toks = tokenize(text)
        if not toks:
            continue

        hints = []

        def run(b, toks_):
            added = []
            hints.clear()
            for t in toks_:
                if re.fullmatch(r"[a-h][1-8]", t):
                    dest = b._sqi(t)
                    alts = [x for x in b.legal() if x[1] == dest and b.sq[x[0]].upper() != "P"]
                    if alts and any(x[1] == dest and b.sq[x[0]].upper() == "P" for x in b.legal()):
                        pc = b.sq[alts[0][0]].upper()
                        nm = {"N": "Knight", "B": "Bishop", "R": "Rook", "Q": "Queen", "K": "King"}[pc]
                        hints.append((t, nm, pc))
                moves = None
                pieces = split_glued(t)
                if pieces:                 
                    bb = b.copy()
                    try:
                        moves = []
                        for p_ in pieces:
                            m = bb.parse_san(p_)
                            moves.append(m)
                            bb.push(m)
                    except ValueError:
                        moves = None
                if moves is None:
                    moves = [b.parse_san(t)]
                for m in moves:
                    added.append(m)
                    b.push(m)
            return added

        hist = state["history"]
        first_err = None
        done = False
        skip = False
        for cut in range(len(hist), -1, -1):
            b2 = Board(state["start_fen"])
            for m in hist[:cut]:
                b2.push(m)
            try:
                added = run(b2, toks)
            except ValueError as e:
                if first_err is None:
                    first_err = e
                continue
            if cut < len(hist):
                existing = hist[cut:cut + len(added)]
                if added == existing:
                    print("  Those moves are already in the game - nothing changed.")
                    skip = True
                    done = True
                    break
                extends = added[:len(hist) - cut] == hist[cut:]
                recent = cut >= len(hist) - 2
                if not (extends or recent or len(added) >= 4):
                    continue     
                if not extends:
                    print("  (updated the game from move %d with what you pasted)" % (cut // 2 + 1))
            state["board"] = b2
            state["history"] = hist[:cut] + added
            done = True
            break
        if not done:
            b = state["board"]
            print("  ", first_err, "-", "White" if b.white else "Black", "is to move.")
            continue
        if skip:
            b = state["board"]
            print("  Waiting for", "White" if b.white else "Black", "- type their move.")
            continue
        for t, nm, pc in hints:
            print("  (I read '%s' as a pawn move. If it was the %s, type:  undo 2  then  %s%s)" % (t, nm, pc, t))
        after_input()

    eng.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
