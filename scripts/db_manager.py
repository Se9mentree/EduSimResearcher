import argparse
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import urlretrieve

import chromadb

from research_agent.config import VECTOR_DB_COLLECTION, VECTOR_DB_DIR
from research_agent.paper_parser import load_paper
from research_agent.rag import retrieve_related_papers, upsert_paper_to_vector_db


COMMANDS = {"ingest", "search", "list", "stats", "delete", "clear"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Manage Chroma vector DB for papers: ingest, list, stats, search, delete, clear."
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    ingest_parser = subparsers.add_parser("ingest", help="Ingest PDFs (file/dir/url/txt list) into Chroma.")
    ingest_parser.add_argument(
        "sources",
        nargs="+",
        help="PDF source(s): local file, directory, .txt list file, or http(s) PDF URL.",
    )
    ingest_parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recursively scan directories for PDFs.",
    )
    ingest_parser.add_argument(
        "--query",
        default="",
        help="Optional retrieval query after ingest.",
    )
    ingest_parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Top-k retrieval size for optional query check.",
    )
    ingest_parser.add_argument(
        "--keep-downloaded",
        action="store_true",
        help="Keep downloaded URL PDFs in a temp folder for debugging.",
    )

    search_parser = subparsers.add_parser("search", help="Query vector DB and print top-k hits.")
    search_parser.add_argument("--query", required=True, help="Search query text.")
    search_parser.add_argument("--top-k", type=int, default=5, help="Top-k hits.")
    search_parser.add_argument(
        "--input-paper",
        default="",
        help="Optional current paper context to append to query.",
    )

    list_parser = subparsers.add_parser("list", help="List papers in vector DB (aggregated by paper_id).")
    list_parser.add_argument("--limit", type=int, default=20, help="Max number of papers to show.")

    subparsers.add_parser("stats", help="Show vector DB summary.")

    delete_parser = subparsers.add_parser("delete", help="Delete paper chunks by metadata key.")
    delete_group = delete_parser.add_mutually_exclusive_group(required=True)
    delete_group.add_argument("--paper-id", help="Delete by paper_id.")
    delete_group.add_argument("--source-path", help="Delete by source_path (exact match).")
    delete_group.add_argument("--title", help="Delete by title (exact match).")
    delete_parser.add_argument("--dry-run", action="store_true", help="Preview deletion count only.")

    clear_parser = subparsers.add_parser("clear", help="Drop the whole collection.")
    clear_parser.add_argument("--yes", action="store_true", help="Confirm destructive clear.")

    raw_args = sys.argv[1:]
    if raw_args and raw_args[0] not in COMMANDS and raw_args[0] not in {"-h", "--help"}:
        raw_args = ["ingest"] + raw_args

    args = parser.parse_args(raw_args)
    if not getattr(args, "command", None):
        parser.print_help()
        raise SystemExit(1)
    return args


def _is_url(text: str) -> bool:
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _iter_pdfs_from_directory(directory: Path, recursive: bool) -> Iterable[Path]:
    pattern = "**/*.pdf" if recursive else "*.pdf"
    for pdf_path in directory.glob(pattern):
        if pdf_path.is_file():
            yield pdf_path.resolve()


def _iter_sources_from_list_file(list_path: Path) -> Iterable[str]:
    for line in list_path.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        yield item


def _download_pdf(url: str, download_dir: Path) -> Path:
    parsed = urlparse(url)
    filename = Path(parsed.path).name or ""
    if not filename.lower().endswith(".pdf"):
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        filename = f"remote_{digest}.pdf"
    target_path = download_dir / filename
    urlretrieve(url, str(target_path))
    return target_path.resolve()


def resolve_pdf_targets(sources: List[str], recursive: bool, download_dir: Path) -> List[Tuple[str, Path]]:
    queue = list(sources)
    targets: List[Tuple[str, Path]] = []

    while queue:
        source = queue.pop(0).strip()
        if not source:
            continue

        if _is_url(source):
            try:
                pdf_path = _download_pdf(source, download_dir)
                print(f"[download] {source} -> {pdf_path}")
                targets.append((source, pdf_path))
            except Exception as e:
                print(f"[skip] URL download failed: {source} ({type(e).__name__}: {e})")
            continue

        path = Path(source).expanduser()
        if not path.exists():
            print(f"[skip] path not found: {source}")
            continue

        if path.is_dir():
            for pdf_path in _iter_pdfs_from_directory(path, recursive=recursive):
                targets.append((str(pdf_path), pdf_path))
            continue

        if path.is_file() and path.suffix.lower() == ".txt":
            queue.extend(_iter_sources_from_list_file(path))
            continue

        if path.is_file() and path.suffix.lower() == ".pdf":
            targets.append((str(path.resolve()), path.resolve()))
            continue

        print(f"[skip] unsupported source type: {source}")

    return targets


def _get_client() -> chromadb.PersistentClient:
    Path(VECTOR_DB_DIR).mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=VECTOR_DB_DIR)


def _collection_exists(client: chromadb.PersistentClient) -> bool:
    names = [c.name for c in client.list_collections()]
    return VECTOR_DB_COLLECTION in names


def _get_collection(client: chromadb.PersistentClient) -> Optional[Any]:
    if not _collection_exists(client):
        return None
    return client.get_collection(VECTOR_DB_COLLECTION)


def _fetch_all_metadatas(collection: Any) -> List[Dict[str, Any]]:
    total = collection.count()
    if total <= 0:
        return []
    metadatas: List[Dict[str, Any]] = []
    offset = 0
    batch_size = 1000
    while offset < total:
        batch = collection.get(
            limit=min(batch_size, total - offset),
            offset=offset,
            include=["metadatas"],
        )
        current = batch.get("metadatas", [])
        if not current:
            break
        metadatas.extend(current)
        offset += len(current)
    return metadatas


def _aggregate_papers(collection: Any) -> List[Dict[str, Any]]:
    paper_map: Dict[str, Dict[str, Any]] = {}
    for meta in _fetch_all_metadatas(collection):
        if not isinstance(meta, dict):
            continue
        paper_id = str(meta.get("paper_id", "unknown"))
        record = paper_map.setdefault(
            paper_id,
            {
                "paper_id": paper_id,
                "title": str(meta.get("title", "")),
                "source_path": str(meta.get("source_path", "")),
                "chunks": 0,
            },
        )
        record["chunks"] += 1
    return sorted(paper_map.values(), key=lambda x: x["chunks"], reverse=True)


def run_ingest_command(args):
    temp_dir = Path(tempfile.mkdtemp(prefix="rag_ingest_"))
    print(f"[info] temp dir: {temp_dir}")

    try:
        targets = resolve_pdf_targets(args.sources, recursive=args.recursive, download_dir=temp_dir)
        if not targets:
            print("[done] no valid PDF targets found.")
            return

        total = len(targets)
        success = 0
        failed = 0

        for index, (source_ref, pdf_path) in enumerate(targets, start=1):
            print(f"\n[{index}/{total}] parsing: {pdf_path}")
            parsed = load_paper(str(pdf_path))
            print(f"[parse] status={parsed.parse_status} notes={parsed.parse_notes}")

            if parsed.parse_status == "failed":
                failed += 1
                continue

            payload = {
                "paper_path": str(pdf_path),
                "source_path": source_ref,
                "title": parsed.title,
                "abstract": parsed.abstract,
                "full_text": parsed.full_text,
                "sections": parsed.sections,
                "key_sections": parsed.key_sections,
            }

            try:
                upsert_paper_to_vector_db(payload)
                success += 1
                print(f"[upsert] ok: {parsed.title or pdf_path.name}")
            except Exception as e:
                failed += 1
                print(f"[upsert] failed: {type(e).__name__}: {e}")

        print(f"\n[summary] total={total} success={success} failed={failed}")
        run_stats_command(args)

        if args.query.strip():
            print(f"\n[verify] query={args.query}")
            hits = retrieve_related_papers(query=args.query, input_paper="", top_k=args.top_k)
            if not hits:
                print("[verify] no hits")
            else:
                for hit in hits:
                    print(f"- {hit}\n")
    finally:
        if args.keep_downloaded:
            print(f"[info] keep downloaded files: {temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)


def run_search_command(args):
    hits = retrieve_related_papers(
        query=args.query.strip(),
        input_paper=args.input_paper.strip(),
        top_k=args.top_k,
    )
    if not hits:
        print("[search] no hits")
        return
    for i, hit in enumerate(hits, start=1):
        print(f"\n===== HIT {i} =====")
        print(hit)


def run_list_command(args):
    client = _get_client()
    collection = _get_collection(client)
    if collection is None:
        print(f"[list] collection not found: {VECTOR_DB_COLLECTION}")
        return
    papers = _aggregate_papers(collection)
    if not papers:
        print("[list] no papers found.")
        return
    show = papers[: max(1, args.limit)]
    print(
        f"[list] collection={VECTOR_DB_COLLECTION} total_papers={len(papers)} total_chunks={collection.count()}"
    )
    for idx, item in enumerate(show, start=1):
        print(
            f"{idx}. paper_id={item['paper_id']} chunks={item['chunks']} "
            f"title={item['title']} source_path={item['source_path']}"
        )


def run_stats_command(_args):
    client = _get_client()
    collections = [c.name for c in client.list_collections()]
    exists = VECTOR_DB_COLLECTION in collections
    print(f"[stats] vector_db_dir={VECTOR_DB_DIR}")
    print(f"[stats] collections={collections}")
    print(f"[stats] active_collection={VECTOR_DB_COLLECTION} exists={exists}")
    if not exists:
        return
    collection = client.get_collection(VECTOR_DB_COLLECTION)
    papers = _aggregate_papers(collection)
    print(f"[stats] total_chunks={collection.count()} total_papers={len(papers)}")


def run_delete_command(args):
    client = _get_client()
    collection = _get_collection(client)
    if collection is None:
        print(f"[delete] collection not found: {VECTOR_DB_COLLECTION}")
        return

    where: Dict[str, Any]
    if args.paper_id:
        where = {"paper_id": args.paper_id}
    elif args.source_path:
        where = {"source_path": args.source_path}
    else:
        where = {"title": args.title}

    matched = collection.get(where=where, include=["metadatas"])
    ids = matched.get("ids", [])
    if not ids:
        print(f"[delete] no records matched where={where}")
        return

    matched_meta = matched.get("metadatas", [])
    unique_papers = sorted(
        {str(meta.get("paper_id", "")) for meta in matched_meta if isinstance(meta, dict) and meta.get("paper_id")}
    )

    print(f"[delete] matched_chunks={len(ids)} unique_papers={len(unique_papers)} where={where}")
    if args.dry_run:
        return

    collection.delete(ids=ids)
    print("[delete] done.")
    run_stats_command(args)


def run_clear_command(args):
    if not args.yes:
        print("[clear] aborted. use --yes to confirm.")
        return
    client = _get_client()
    if not _collection_exists(client):
        print(f"[clear] collection not found: {VECTOR_DB_COLLECTION}")
        return
    client.delete_collection(VECTOR_DB_COLLECTION)
    print(f"[clear] deleted collection: {VECTOR_DB_COLLECTION}")
    run_stats_command(args)


def main():
    args = parse_args()
    if args.command == "ingest":
        run_ingest_command(args)
        return
    if args.command == "search":
        run_search_command(args)
        return
    if args.command == "list":
        run_list_command(args)
        return
    if args.command == "stats":
        run_stats_command(args)
        return
    if args.command == "delete":
        run_delete_command(args)
        return
    if args.command == "clear":
        run_clear_command(args)
        return
    raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
