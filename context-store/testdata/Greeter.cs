namespace TestData;

public static class Greeter
{
    public static string Hello(string name)
    {
        return "Hello, " + name + "! The answer is " + Compute(6, 7).ToString() + ".";
    }

    public static int Compute(int a, int b)
    {
        return a * b;
    }

    public static string Describe()
    {
        return "I was decomposed into nodes, stored in PostgreSQL, regenerated from the DB, compiled in memory, and executed via reflection.";
    }
}
