from flask import Flask, jsonify, request

app = Flask(__name__)

# Database in memoria (per il prototipo dell'hackathon)
# Qui accumuliamo i dati ricevuti dai nodi sparsi per la città
traffic_data_store = []


@app.route("/", methods=["GET"])
def home():
  return jsonify({
      "status": "online",
      "message": "Smart Crossing Backend Server is running!",
  })


@app.route("/api/traffic", methods=["POST"])
def receive_traffic_data():
  """Endpoint API per ricevere i dati dai nodi smart crossing (peer).

  Attende un JSON strutturato così: { "node_id": "node_01", "coordinates": {"lat":
  45.0703, "lon": 7.6869}, "counts": {"pedestrians": 12, "cyclists": 5,
  "ambulances": 1} }
  """
  data = request.get_json()

  if not data:
    return jsonify({"error": "Invalid JSON or missing payload"}), 400

  # Estraiamo i campi principali
  node_id = data.get("node_id")
  coordinates = data.get("coordinates")
  counts = data.get("counts")

  if not node_id or not counts:
    return jsonify({"error": "Missing required fields (node_id or counts)"}), 400

  # Salviamo il record ricevuto (in un caso reale andrebbe su un DB persistente)
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
          "total_records_stored": len(traffic_data_store),
      }),
      201,
  )


@app.route("/api/traffic", methods=["GET"])
def get_traffic_data():
  """Endpoint per restituire tutti i dati accumulati (utilizzabile per la heatmap)"""
  return jsonify(traffic_data_store), 200


if __name__ == "__main__":
  app.run(debug=True, port=5000)