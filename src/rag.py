from pathlib import Path
from langchain_text_splitters import MarkdownHeaderTextSplitter
from sentence_transformers import SentenceTransformer
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct
import yaml


with open("config.yaml") as f:
    config = yaml.safe_load(f)

DOCS_DIR = Path(config["docs_dir"])
COLLECTION = config["collection"]

embedder = SentenceTransformer(config["embedding_model"])
client = QdrantClient(url=config["qdrant_url"])


splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=[("#", "title"), ("##", "section")]
)

def load_and_split():
    chunks = []
    for path in sorted(DOCS_DIR.glob("*.md")):
        sections = splitter.split_text(path.read_text())
        alert_type = next(
            s.page_content.strip() for s in sections if s.metadata["section"] == "Alert Type"
        )
        # print(sections)
        for s in sections:
            chunks.append({
                "text": f"{s.metadata['title']} > {s.metadata['section']}:\n{s.page_content}",
                "source": path.name,
                "alert_type": alert_type,
                "section": s.metadata["section"],
            })
    return chunks


def ingest():
    chunks = load_and_split()
    # print(chunks)

    if client.collection_exists(COLLECTION):
        client.delete_collection(COLLECTION)
    client.create_collection(
        collection_name=COLLECTION,
        vectors_config=VectorParams(size=embedder.get_sentence_embedding_dimension(), distance=Distance.COSINE),
    )

    vectors = embedder.encode([c["text"] for c in chunks])
    points = [
        PointStruct(id=i, vector=vectors[i].tolist(), payload=chunks[i])
        for i in range(len(chunks))
    ]
    client.upsert(collection_name=COLLECTION, points=points)
    print(f"stored {client.count(COLLECTION).count} chunks")

if __name__ == "__main__":
    ingest()