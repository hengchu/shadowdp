int numsparsevectorN(float epsilon, int size, float q[], float T, float NN)
{
  "ALL_DIFFER; assume(NN > 0)";
  "epsilon: <0, 0>; size: <0, 0>; q: <*, *>; T: <0, 0>; NN: <0, 0>";
  int out = 0;
  float eta_1 = Lap(3.0 / epsilon, "ALIGNED; 1");
  float T_bar = T + eta_1;
  float count = 0;
  int i = 0;

  while (count <= NN - 1 && i < size)
  {
    float eta_2 = Lap((6.0 * NN) / epsilon, "ALIGNED; (q[i] + eta_2 >= T_bar) ? 2 : 0;");
    float eta_3 = Lap((3.0 * NN) / epsilon, "ALIGNED; (q[i] + eta_2 >= T_bar) ? -(__SHADOWDP_ALIGNED_DISTANCE_q[i]) : 0;");
    if (q[i] + eta_2 >= T_bar)
    {
      out = q[i] + eta_3;
      count = count + 1;
    }
    else
    {
      out = 0;
    }
    i = i + 1;
  }
  return out;
}
