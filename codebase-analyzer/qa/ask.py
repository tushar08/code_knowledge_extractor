"""Ask questions over the extracted knowledge base.

Usage:
    python -m qa.ask --question "How does authentication work?"
    python -m qa.ask --interactive
    python -m qa.ask -q "Which methods are most complex?" --mode bm25 --top-k 4
"""
import argparse

from .engine import answer, build_retriever, load_nodes


def main():
    ap = argparse.ArgumentParser(description="Q&A over knowledge.json")
    ap.add_argument("--knowledge", default="output/knowledge.json")
    ap.add_argument("-q", "--question")
    ap.add_argument("--mode", choices=["auto", "vector", "bm25"], default="auto")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--interactive", action="store_true")
    args = ap.parse_args()

    nodes = load_nodes(args.knowledge)
    retriever, mode = build_retriever(nodes, args.mode, args.top_k)
    print(f"[ready] {len(nodes)} knowledge nodes indexed (retrieval: {mode})\n")

    def ask(q: str):
        hits = retriever.retrieve(q)
        print(f"Q: {q}\n")
        print(answer(q, hits))
        print("\n" + "=" * 72 + "\n")

    if args.question:
        ask(args.question)
    if args.interactive:
        while True:
            try:
                q = input("question> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q.lower() in ("", "exit", "quit"):
                break
            ask(q)


if __name__ == "__main__":
    main()
