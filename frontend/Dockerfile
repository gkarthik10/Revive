FROM node:20-alpine

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .

EXPOSE 5173

# --host 0.0.0.0 so the dev server is reachable from outside the container;
# the browser still hits it at http://localhost:5173, which is what the
# backend's CORS config and this project's docs already assume.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
