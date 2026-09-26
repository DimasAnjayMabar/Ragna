from neo4j import GraphDatabase

URI = "neo4j://127.0.0.1:7687"
USER = "neo4j"
PASSWORD = "password"

driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))

with driver.session() as session:
    # Total semua node
    total = session.run("MATCH (n) RETURN count(n) AS total").single()["total"]
    print(f"Total node: {total}")

    # Node per label
    labels = session.run("CALL db.labels() YIELD label RETURN label").data()
    for row in labels:
        label = row["label"]
        count = session.run(f"MATCH (n:`{label}`) RETURN count(n) AS c").single()["c"]
        print(f"  [{label}]: {count} node")

driver.close()