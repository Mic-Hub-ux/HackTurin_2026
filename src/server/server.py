from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# Database in memoria (per il prototipo dell'hackathon)
# Qui accumuliamo i dati ricevuti dai nodi sparsi per la città
traffic_data_store = []


@app.route("/", methods=["GET"])
def home():
  return render_template("index.html")


@app.route("/cyclist", methods=["GET"])
def cyclist_view():
  return render_template("cyclist.html")


@app.route("/api/traffic", methods=["POST"])
def receive_traffic_data():
  """Endpoint API per ricevere i dati dai nodi smart crossing (peer)."""
  data = request.get_json()

  if not data:
    return jsonify({"error": "Invalid JSON or missing payload"}), 400

  node_id = data.get("node_id")
  coordinates = data.get("coordinates")
  counts = data.get("counts")

  if not node_id or not counts:
    return jsonify({"error": "Missing required fields (node_id or counts)"}), 400

  # Check if the node already exists in our store
  existing_node = next(
      (item for item in traffic_data_store if item["node_id"] == node_id), None
  )

  if existing_node:
    # Update existing node's data with the latest payload
    if coordinates:
      existing_node["coordinates"] = coordinates
    existing_node["counts"] = counts
  else:
    # Add a new record if it doesn't exist yet
    record = {
        "node_id": node_id,
        "coordinates": coordinates,
        "counts": counts,
    }
    traffic_data_store.append(record)

  print(f"[DATA RECEIVED] From Node {node_id}: {counts}")

  return (
      jsonify({
          "status": "success",
          "message": "Data recorded successfully",
          "total_active_nodes": len(traffic_data_store),
      }),
      201,
  )


@app.route("/api/traffic", methods=["GET"])
def get_traffic_data():
  """Endpoint per restituire tutti i dati accumulati (utilizzabile per la mappa)"""
  return jsonify(traffic_data_store), 200


@app.route("/api/traffic/<node_id>", methods=["GET"])
def get_node_traffic(node_id):
  """API endpoint to fetch the latest traffic data for a specific node/intersection."""
  node_data = next(
      (item for item in traffic_data_store if item["node_id"] == node_id), None
  )

  if not node_data:
    return jsonify({"error": f"Node '{node_id}' not found or inactive"}), 404

  return jsonify(node_data), 200


if __name__ == "__main__":
  app.run(debug=True, port=5000)