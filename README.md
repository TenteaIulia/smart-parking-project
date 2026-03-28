# smart-parking-project
Smart Parking is a web application for intelligent parking management, developed using Flask (Python) and MySQL.

Backend:
- Flask for routing and application logic
- MySQL for data storage (reservations, sessions, users, zones)
- mysql-connector for database connection
- Complete management of flows: reservation, cancellation, expiration, barrier access, exit, and cost calculation

Frontend:
- HTML + CSS for interface
- Jinja2 for dynamic rendering
- Responsive design for desktop and mobile
- Features: reservation filtering, statuses, flash messages

Main features:
- Parking space reservation for time intervals
- Detection of expired reservations (no-show)
- Automatic cost and penalty calculation
- Access to parking with or without reservation
- Active session management
- Admin dashboard with statistics

Hardware component:
- ESP32-CAM for license plate recognition
- Arduino + servo for barrier simulation
- API integration between hardware and web application
