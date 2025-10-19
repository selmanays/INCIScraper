import { ImageResponse } from "next/server";

export const size = {
  width: 64,
  height: 64,
};

export const contentType = "image/png";

export default function Icon() {
  return new ImageResponse(
    (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          width: "100%",
          height: "100%",
          borderRadius: "20%",
          fontSize: 28,
          fontWeight: 700,
          background:
            "linear-gradient(135deg, rgb(79 70 229), rgb(14 165 233))",
          color: "white",
        }}
      >
        IN
      </div>
    ),
    {
      ...size,
    }
  );
}
