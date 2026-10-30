from fastapi import FastAPI

app = FastAPI()


@app.get("/health/ready")
def readiness():
    return {"status": "ready"}


def calculate_invoice_total(items):
    return sum(item["price"] * item["quantity"] for item in items)
