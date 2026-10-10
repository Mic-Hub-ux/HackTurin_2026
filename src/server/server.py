from flask import Flask, jsonify, request

app = Flask(__name__)

# In-memory database for the hackathon prototype
# Stores traffic data received from smart crossing nodes
traffic_data_store = []


@app.route("/", methods=["GET"])
def home():
  return jsonify({
      "status": "online",
      "message": "Smart Crossing Backend Server is running!",
  })


@app.route("/api/traffic", methods=["POST"])
def receive_traffic_data():
  """API endpoint to receive traffic data from smart crossing peer nodes.

  Expected JSON format: { "node_id": "node_01", "counts": {"pedestrians": 12,
  "cyclists": 5, "ambulances": 1} }
  """
  data = request.get_json()

  if not data:
    return jsonify({"error": "Invalid JSON or missing payload"}), 400

  # Extract main fields
  node_id = data.get("node_id")
  counts = data.get("counts")

  if not node_id or not counts:
    return jsonify({"error": "Missing required fields (node_id or counts)"}), 400

  # Save the received record
  record = {
      "node_id": node_id,
      "counts": counts,
  }
  traffic_data_store.append(record)

  print(f"[DATA RECEIVED] From Node {node_id}: {counts}")

  return (
      jsonify({
          "status": "success",
          "message": "Data recorded successfully",
          "total_records_stored": len(traffic_data_store),
      }),
      201,
  )


@app.route("/api/traffic", methods=["GET"])
def get_traffic_data():
  """Endpoint to retrieve all accumulated traffic data"""
  return jsonify(traffic_data_store), 200


if __name__ == "__main__":
  app.run(debug=True, port=5000)