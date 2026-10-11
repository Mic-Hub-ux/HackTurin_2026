from flask import Flask, render_template, request, jsonify

app = Flask(__name__, template_folder='templates', static_folder='static')

# Segreto condiviso per la sicurezza delle comunicazioni IoT
API_SECRET_TOKEN = "efesto-secure-iot-key-2026"

# Memoria in-memory per i dati dei nodi
nodes_data = {}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/cyclist')
def cyclist():
    return render_template('cyclist.html')

@app.route('/api/traffic', methods=['GET', 'POST'])
def traffic():
    if request.method == 'POST':
        # Controllo di sicurezza: verifica il token nell'header Authorization
        auth_header = request.headers.get('Authorization')
        expected_auth = f"Bearer {API_SECRET_TOKEN}"
        
        if not auth_header or auth_header != expected_auth:
            return jsonify({"error": "Unauthorized - Invalid or missing token"}), 403

        data = request.json
        if not data or 'node_id' not in data:
            return jsonify({"error": "Invalid payload"}), 400
        
        node_id = data['node_id']
        nodes_data[node_id] = data
        return jsonify({"status": "success", "node_id": node_id}), 200

    elif request.method == 'GET':
        # Restituisce la lista dei nodi attivi per le dashboard (index e cyclist)
        return jsonify(list(nodes_data.values())), 200

@app.route('/api/traffic/<node_id>', methods=['GET'])
def node_traffic(node_id):
    node = nodes_data.get(node_id)
    if not node:
        return jsonify({"error": "Node not found"}), 404
    return jsonify(node), 200

if __name__ == '__main__':
    app.run(debug=True)