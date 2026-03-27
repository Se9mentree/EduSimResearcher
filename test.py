# import chromadb
# from research_agent.config import VECTOR_DB_DIR, VECTOR_DB_COLLECTION

# client = chromadb.PersistentClient(path=VECTOR_DB_DIR)
# print("collections:", [c.name for c in client.list_collections()])

# col = client.get_collection(VECTOR_DB_COLLECTION)
# print("count:", col.count())

# sample = col.get(limit=3, include=["documents", "metadatas"])
# for i, (doc, meta) in enumerate(zip(sample["documents"], sample["metadatas"]), 1):
#     print(f"\n--- sample #{i} ---")
#     print("meta:", meta)
#     print("doc:", (doc or "")[:300])

from research_agent.rag import retrieve_related_papers

query = "How do multi-agent systems improve personalized learning outcomes?"
hits = retrieve_related_papers(query=query, input_paper="", top_k=5)

for i, h in enumerate(hits, 1):
    print(f"\n===== HIT {i} =====")
    print(h)
