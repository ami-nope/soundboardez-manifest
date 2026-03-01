from flask import Flask, jsonify

app = Flask(__main__)

@app.route("/manifest")
def manifest():
    return jsonify({
        "version": "1.3.0",
        "mandatory": False,
        "files": {
            "SoundboardEZ.exe": {
                "url": "https://yourcdn.com/1.3.0/SoundboardEZ.exe",
                "sha256": "PUT_REAL_HASH_HERE"
            }
        }
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
