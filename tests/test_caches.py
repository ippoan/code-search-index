import struct

from indexer import caches, chunker, db as dbm


def _vec(seed):
    return [seed] * dbm.DIMS


def _build_index(path):
    db = dbm.open_db(str(path))
    dbm.set_meta(db, "model", dbm.MODEL_NAME)
    ch1 = chunker.Chunk(start_line=1, end_line=3, symbol="f", text="fn f() {}")
    ch2 = chunker.Chunk(start_line=5, end_line=9, symbol="g", text="fn g() {}")
    dbm.insert_chunk(db, "org/r1", "src/a.rs", ch1, "rust", _vec(0.1))
    dbm.insert_chunk(db, "org/r1", "src/a.rs", ch2, "rust", _vec(0.2))
    dbm.set_repo_commit(db, "org/r1", "abc123", "2026-08-26T00:00:00+00:00")
    db.commit()
    db.close()


def test_build_and_reuse_caches(tmp_path):
    _build_index(tmp_path / "index.db")
    chunks_gz = tmp_path / "chunks.json.gz"
    vectors = tmp_path / "vectors.db"
    caches.build_caches(str(tmp_path / "index.db"), str(chunks_gz), str(vectors))

    # chunk cache: shard-side reuse reconstructs identical Chunk objects
    data = caches.load_chunk_cache(str(chunks_gz))
    entry = data["org/r1"]
    assert entry["sha"] == "abc123"
    assert len(entry["chunks"]) == 2
    path, s, e, sym, lang, text = entry["chunks"][0]
    ch = chunker.Chunk(start_line=s, end_line=e, symbol=sym, text=text)
    assert (path, lang) == ("src/a.rs", "rust")
    assert ch == chunker.Chunk(start_line=1, end_line=3, symbol="f", text="fn f() {}")

    # vector cache: keyed by the shared embed_text, returns the packed blob
    vc = caches.open_vector_cache(str(vectors))
    key = caches.text_hash(chunker.embed_text("org/r1", "src/a.rs", "fn f() {}"))
    blob = caches.lookup_vector(vc, key)
    assert blob is not None
    assert abs(struct.unpack(f"{dbm.DIMS}f", blob)[0] - 0.1) < 1e-6
    assert caches.lookup_vector(vc, caches.text_hash("no-such-text")) is None


def test_missing_chunk_cache_is_empty(tmp_path):
    assert caches.load_chunk_cache(str(tmp_path / "nope.json.gz")) == {}
    assert caches.load_chunk_cache("") == {}
