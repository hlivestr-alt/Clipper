const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");

const assetsDirectory = path.resolve(__dirname, "..", "assets");
const masterSize = 1024;
const iconSizes = [16, 24, 32, 48, 64, 128, 256];

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function crc32(buffer) {
  let crc = 0xffffffff;
  for (const byte of buffer) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function pngChunk(type, data) {
  const typeBuffer = Buffer.from(type, "ascii");
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE(crc32(Buffer.concat([typeBuffer, data])), 0);
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length, 0);
  return Buffer.concat([length, typeBuffer, data, checksum]);
}

function encodePng(rgba, size) {
  const scanlines = Buffer.alloc(size * (size * 4 + 1));
  for (let y = 0; y < size; y += 1) {
    const rowOffset = y * (size * 4 + 1);
    scanlines[rowOffset] = 0;
    rgba.copy(scanlines, rowOffset + 1, y * size * 4, (y + 1) * size * 4);
  }

  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header[8] = 8;
  header[9] = 6;
  header[10] = 0;
  header[11] = 0;
  header[12] = 0;
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    pngChunk("IHDR", header),
    pngChunk("IDAT", zlib.deflateSync(scanlines, { level: 9 })),
    pngChunk("IEND", Buffer.alloc(0))
  ]);
}

function roundedRectangleContains(x, y, left, top, right, bottom, radius) {
  const nearestX = clamp(x, left + radius, right - radius);
  const nearestY = clamp(y, top + radius, bottom - radius);
  return (x - nearestX) ** 2 + (y - nearestY) ** 2 <= radius ** 2;
}

function renderIcon(size) {
  const supersample = 4;
  const highSize = size * supersample;
  const high = Buffer.alloc(highSize * highSize * 4);
  const tileInset = highSize * 0.09;
  const tileRadius = highSize * 0.2;
  const center = highSize / 2;
  const markRadius = highSize * 0.28;
  const markStroke = Math.max(highSize * 0.072, supersample);
  const dotRadius = Math.max(highSize * 0.04, supersample * 0.55);
  const dotAngle = (40 * Math.PI) / 180;
  const dotCenterX = center + Math.cos(dotAngle) * markRadius;
  const dotCenterY = center + Math.sin(dotAngle) * markRadius;

  for (let y = 0; y < highSize; y += 1) {
    for (let x = 0; x < highSize; x += 1) {
      const pixelX = x + 0.5;
      const pixelY = y + 0.5;
      const tile = roundedRectangleContains(
        pixelX,
        pixelY,
        tileInset,
        tileInset,
        highSize - tileInset,
        highSize - tileInset,
        tileRadius
      );
      if (!tile) {
        continue;
      }

      const offset = (y * highSize + x) * 4;
      high[offset] = 9;
      high[offset + 1] = 9;
      high[offset + 2] = 11;
      high[offset + 3] = 255;

      const dx = pixelX - center;
      const dy = pixelY - center;
      const distance = Math.sqrt(dx * dx + dy * dy);
      const angle = (Math.atan2(dy, dx) * 180) / Math.PI;
      const normalizedAngle = angle < 0 ? angle + 360 : angle;
      const arc = normalizedAngle >= 42 && normalizedAngle <= 318
        && Math.abs(distance - markRadius) <= markStroke / 2;
      const dot = (pixelX - dotCenterX) ** 2 + (pixelY - dotCenterY) ** 2 <= dotRadius ** 2;
      if (arc || dot) {
        high[offset] = 255;
        high[offset + 1] = 255;
        high[offset + 2] = 255;
      }
    }
  }

  const output = Buffer.alloc(size * size * 4);
  const samples = supersample ** 2;
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      let red = 0;
      let green = 0;
      let blue = 0;
      let alpha = 0;
      for (let sampleY = 0; sampleY < supersample; sampleY += 1) {
        for (let sampleX = 0; sampleX < supersample; sampleX += 1) {
          const highOffset = ((y * supersample + sampleY) * highSize + x * supersample + sampleX) * 4;
          const sampleAlpha = high[highOffset + 3] / 255;
          red += high[highOffset] * sampleAlpha;
          green += high[highOffset + 1] * sampleAlpha;
          blue += high[highOffset + 2] * sampleAlpha;
          alpha += sampleAlpha;
        }
      }
      const outputOffset = (y * size + x) * 4;
      const averageAlpha = alpha / samples;
      output[outputOffset] = averageAlpha ? Math.round(red / alpha) : 0;
      output[outputOffset + 1] = averageAlpha ? Math.round(green / alpha) : 0;
      output[outputOffset + 2] = averageAlpha ? Math.round(blue / alpha) : 0;
      output[outputOffset + 3] = Math.round(averageAlpha * 255);
    }
  }
  return output;
}

function encodeIco(images) {
  const directory = Buffer.alloc(6 + images.length * 16);
  directory.writeUInt16LE(0, 0);
  directory.writeUInt16LE(1, 2);
  directory.writeUInt16LE(images.length, 4);
  let offset = directory.length;
  const payloads = [];
  images.forEach(({ size, data }, index) => {
    const entryOffset = 6 + index * 16;
    directory[entryOffset] = size === 256 ? 0 : size;
    directory[entryOffset + 1] = size === 256 ? 0 : size;
    directory[entryOffset + 2] = 0;
    directory[entryOffset + 3] = 0;
    directory.writeUInt16LE(1, entryOffset + 4);
    directory.writeUInt16LE(32, entryOffset + 6);
    directory.writeUInt32LE(data.length, entryOffset + 8);
    directory.writeUInt32LE(offset, entryOffset + 12);
    payloads.push(data);
    offset += data.length;
  });
  return Buffer.concat([directory, ...payloads]);
}

fs.mkdirSync(assetsDirectory, { recursive: true });
const masterPng = encodePng(renderIcon(masterSize), masterSize);
fs.writeFileSync(path.join(assetsDirectory, "icon.png"), masterPng);

const icoImages = iconSizes.map((size) => ({
  size,
  data: encodePng(renderIcon(size), size)
}));
fs.writeFileSync(path.join(assetsDirectory, "icon.ico"), encodeIco(icoImages));
console.log(`Generated ${path.join(assetsDirectory, "icon.png")}`);
console.log(`Generated ${path.join(assetsDirectory, "icon.ico")} with sizes ${iconSizes.join(", ")}px`);
