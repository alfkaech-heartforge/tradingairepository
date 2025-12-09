from Flask import Flask, request; 
app = Flask(__name__); 
@app.route('/webhook', methods=['POST']) 
def webhook(): 
data = request.json; 
print('payload:', data)'; 
with open('log.txt','a')' as f: f.write(str(data) + '\n'); 
return 'Logged!', 200 
