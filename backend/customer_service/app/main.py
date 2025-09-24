import logging
import os
import sys
import time
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

# --- Prometheus client imports ---
from prometheus_client import Counter, Histogram, Gauge, generate_latest
from prometheus_client.core import CollectorRegistry
from starlette.responses import PlainTextResponse # Required for /metrics endpoint

from .db import Base, engine, get_db
from .models import Customer
from .schemas import CustomerCreate, CustomerResponse, CustomerUpdate

# --- Standard Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Suppress noisy logs from third-party libraries for cleaner output
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.INFO)


PRODUCT_SERVICE_URL = os.getenv("PRODUCT_SERVICE_URL", "http://localhost:8000")
logger.info(
    f"Order Service: Configured to communicate with Product Service at: {PRODUCT_SERVICE_URL}"
)


# --- Prometheus Metrics Initialization ---
# Create a custom registry specific to this application instance
registry = CollectorRegistry()
APP_NAME = "customer_service" # Unique identifier for this service in metrics

# Define Prometheus metrics (Basic HTTP Metrics)
REQUEST_COUNT = Counter(
    'http_requests_total', 'Total HTTP requests processed by the application',
    ['app_name', 'method', 'endpoint', 'status_code'], registry=registry
)
REQUEST_DURATION = Histogram(
    'http_request_duration_seconds', 'HTTP request duration in seconds',
    ['app_name', 'method', 'endpoint', 'status_code'], registry=registry
)
REQUESTS_IN_PROGRESS = Gauge(
    'http_requests_in_progress', 'Number of HTTP requests in progress',
    ['app_name', 'method', 'endpoint'], registry=registry
)

# Custom Metrics specific to Order Service business logic
ORDER_CREATION_TOTAL = Counter(
    'order_creation_total', 'Total number of orders created',
    ['app_name', 'status'], registry=registry # status: success, failed_items, db_error
)
ORDER_ITEM_COUNT = Counter(
    'order_item_count', 'Total number of individual items processed in orders',
    ['app_name', 'product_id'], registry=registry
)
ORDER_TOTAL_AMOUNT = Histogram(
    'order_total_amount_dollars', 'Total amount of orders in dollars',
    ['app_name'], registry=registry # This will provide buckets for order value distribution
)
ORDER_STATUS_UPDATE_TOTAL = Counter(
    'order_status_update_total', 'Total order status updates',
    ['app_name', 'status'], registry=registry # status: success, not_found, db_error
)
# Metrics for inter-service communication (calls from Order Service to Product Service)
PRODUCT_SERVICE_CALL_TOTAL = Counter(
    'product_service_call_total', 'Total calls made from Order Service to Product Service',
    ['app_name', 'target_endpoint', 'method', 'status_code'], registry=registry
)
PRODUCT_SERVICE_CALL_DURATION = Histogram(
    'product_service_call_duration_seconds', 'Duration of calls from Order Service to Product Service',
    ['app_name', 'target_endpoint', 'method', 'status_code'], registry=registry
)


# --- Middleware for Prometheus Metrics ---
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    # Exclude the /metrics endpoint itself from being tracked
    if request.url.path == "/metrics":
        response = await call_next(request)
        return response

    method = request.method
    endpoint = request.url.path

    # Increment requests in progress
    REQUESTS_IN_PROGRESS.labels(app_name=APP_NAME, method=method, endpoint=endpoint).inc()
    start_time = time.time()
    
    response = await call_next(request) # Process the actual request

    process_time = time.time() - start_time
    status_code = response.status_code

    # Decrement requests in progress
    REQUESTS_IN_PROGRESS.labels(app_name=APP_NAME, method=method, endpoint=endpoint).dec()
    # Increment total requests
    REQUEST_COUNT.labels(app_name=APP_NAME, method=method, endpoint=endpoint, status_code=status_code).inc()
    # Observe duration for request latency
    REQUEST_DURATION.labels(app_name=APP_NAME, method=method, endpoint=endpoint, status_code=status_code).observe(process_time)

    return response

# --- Prometheus Metrics Endpoint ---
# This is the endpoint Prometheus will scrape to collect metrics.
@app.get("/metrics", response_class=PlainTextResponse, summary="Prometheus metrics endpoint")
async def metrics():
    # generate_latest collects all metrics from the registry and formats them for Prometheus
    return PlainTextResponse(generate_latest(registry))

# --- FastAPI Application Setup ---
app = FastAPI(
    title="Customer Service API",
    description="Manages orders for mini-ecommerce app, with synchronous stock deduction.",
    version="1.0.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- FastAPI Event Handlers ---
@app.on_event("startup")
async def startup_event():
    max_retries = 10
    retry_delay_seconds = 5
    for i in range(max_retries):
        try:
            logger.info(
                f"Customer Service: Attempting to connect to PostgreSQL and create tables (attempt {i+1}/{max_retries})..."
            )
            Base.metadata.create_all(bind=engine)
            logger.info(
                "Customer Service: Successfully connected to PostgreSQL and ensured tables exist."
            )
            break  # Exit loop if successful
        except OperationalError as e:
            logger.warning(f"Customer Service: Failed to connect to PostgreSQL: {e}")
            if i < max_retries - 1:
                logger.info(
                    f"Customer Service: Retrying in {retry_delay_seconds} seconds..."
                )
                time.sleep(retry_delay_seconds)
            else:
                logger.critical(
                    f"Customer Service: Failed to connect to PostgreSQL after {max_retries} attempts. Exiting application."
                )
                sys.exit(1)  # Critical failure: exit if DB connection is unavailable
        except Exception as e:
            logger.critical(
                f"Customer Service: An unexpected error occurred during database startup: {e}",
                exc_info=True,
            )
            sys.exit(1)


# --- Root Endpoint ---
@app.get("/", status_code=status.HTTP_200_OK, summary="Root endpoint")
async def read_root():
    return {"message": "Welcome to the Customer Service!"}


# --- Health Check Endpoint ---
@app.get("/health", status_code=status.HTTP_200_OK, summary="Health check endpoint")
async def health_check():
    return {"status": "ok", "service": "customer-service"}


# --- CRUD Endpoints for Customers ---
@app.post(
    "/customers/",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new customer",
)
async def create_customer(customer: CustomerCreate, db: Session = Depends(get_db)):
    logger.info(f"Customer Service: Creating customer with email: {customer.email}")
    db_customer = Customer(
        email=customer.email,
        password_hash=customer.password,  # Storing raw password for simplicity in this example
        first_name=customer.first_name,
        last_name=customer.last_name,
        phone_number=customer.phone_number,
        shipping_address=customer.shipping_address,
    )

    try:
        db.add(db_customer)
        db.commit()
        db.refresh(db_customer)
        logger.info(
            f"Customer Service: Customer '{db_customer.email}' (ID: {db_customer.customer_id}) created successfully."
        )
        return db_customer
    except IntegrityError:
        db.rollback()
        logger.warning(
            f"Customer Service: Attempted to create customer with existing email: {customer.email}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered."
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Customer Service: Error creating customer: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not create customer.",
        )


@app.get(
    "/customers/",
    response_model=List[CustomerResponse],
    summary="Retrieve a list of all customers",
)
def list_customers(
    db: Session = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    search: Optional[str] = Query(None, max_length=255),
):
    logger.info(
        f"Customer Service: Listing customers with skip={skip}, limit={limit}, search='{search}'"
    )
    query = db.query(Customer)
    if search:
        search_pattern = f"%{search}%"
        logger.info(f"Customer Service: Applying search filter for term: {search}")
        query = query.filter(
            (Customer.first_name.ilike(search_pattern))
            | (Customer.last_name.ilike(search_pattern))
            | (Customer.email.ilike(search_pattern))
        )
    customers = query.offset(skip).limit(limit).all()

    logger.info(
        f"Customer Service: Retrieved {len(customers)} customers (skip={skip}, limit={limit})."
    )
    return customers


@app.get(
    "/customers/{customer_id}",
    response_model=CustomerResponse,
    summary="Retrieve a single customer by ID",
)
def get_customer(customer_id: int, db: Session = Depends(get_db)):
    """
    Retrieves details for a specific customer using their unique ID.
    """
    logger.info(f"Customer Service: Fetching customer with ID: {customer_id}")
    customer = db.query(Customer).filter(Customer.customer_id == customer_id).first()
    if not customer:
        logger.warning(f"Customer Service: Customer with ID {customer_id} not found.")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
        )

    logger.info(
        f"Customer Service: Retrieved customer with ID {customer_id}. Email: {customer.email}"
    )
    return customer


@app.put(
    "/customers/{customer_id}",
    response_model=CustomerResponse,
    summary="Update an existing customer by ID",
)
async def update_customer(
    customer_id: int, customer_data: CustomerUpdate, db: Session = Depends(get_db)
):
    """
    Updates an existing customer's details. Only provided fields will be updated.
    Does not allow password update via this endpoint for security (use a dedicated endpoint if needed).
    """
    logger.info(
        f"Customer Service: Updating customer with ID: {customer_id} with data: {customer_data.model_dump(exclude_unset=True)}"
    )
    db_customer = db.query(Customer).filter(Customer.customer_id == customer_id).first()
    if not db_customer:
        logger.warning(
            f"Customer Service: Attempted to update non-existent customer with ID {customer_id}."
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
        )

    update_data = customer_data.model_dump(exclude_unset=True)

    if "password" in update_data:  # If 'password' was somehow passed, remove it
        logger.warning(
            f"Customer Service: Attempted password update via general /customers/{{id}} endpoint for customer {customer_id}. This is disallowed."
        )
        del update_data["password"]  # Remove password if present

    for key, value in update_data.items():
        setattr(db_customer, key, value)

    try:
        db.add(db_customer)  # Mark for update
        db.commit()
        db.refresh(db_customer)
        logger.info(f"Customer Service: Customer {customer_id} updated successfully.")
        return db_customer
    except IntegrityError:
        db.rollback()
        # This could happen if a user tries to change email to one that already exists
        logger.warning(
            f"Customer Service: Attempted to update customer {customer_id} to an existing email."
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Updated email already registered by another customer.",
        )
    except Exception as e:
        db.rollback()
        logger.error(
            f"Customer Service: Error updating customer {customer_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not update customer.",
        )


@app.delete(
    "/customers/{customer_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a customer by ID",
)
def delete_customer(customer_id: int, db: Session = Depends(get_db)):
    """
    Deletes a customer record from the database.
    """
    logger.info(
        f"Customer Service: Attempting to delete customer with ID: {customer_id}"
    )
    customer = db.query(Customer).filter(Customer.customer_id == customer_id).first()
    if not customer:
        logger.warning(
            f"Customer Service: Attempted to delete non-existent customer with ID {customer_id}."
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
        )

    try:
        db.delete(customer)
        db.commit()
        logger.info(
            f"Customer Service: Customer {customer_id} deleted successfully. Email: {customer.email}"
        )
    except Exception as e:
        db.rollback()
        logger.error(
            f"Customer Service: Error deleting customer {customer_id}: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while deleting the customer.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
