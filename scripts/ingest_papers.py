import argparse
import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, List, Tuple
from urllib.parse import urlparse
from urllib.request import urlretrieve

from research_agent.paper_parser import load_paper
from research_agent.rag import retrieve_related_papers, upsert_paper_to_vector_db


def parse_args():
    parser = argparse.ArgumentParser(
        description="Ingest PDFs into Chroma vector DB via pdf-reader-mcp parser."
    )
    parser.add_argument(
        "sources",
        nargs="+",
        help="PDF source(s): local file, directory, .txt list file, or http(s) PDF URL.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recursively scan directories for PDFs.",
    )
    parser.add_argument(
        "--query",
        default="",
        help="Optional: run one retrieval query after ingest to verify RAG.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Top-k retrieval size for optional query check.",
    )
    parser.add_argument(
        "--keep-downloaded",
        action="store_true",
        help="Keep downloaded URL PDFs in a temp folder for debugging.",
    )
    return parser.parse_args()


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


def main():
    args = parse_args()
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


if __name__ == "__main__":
    main()
