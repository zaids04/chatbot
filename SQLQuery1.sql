USE TestDB;
GO

-- Create the table (dbo schema)
IF OBJECT_ID('dbo.WasteData') IS NOT NULL
    DROP TABLE dbo.WasteData;
GO

CREATE TABLE dbo.WasteData (
    City           NVARCHAR(100) NOT NULL,
    [Year]         INT           NOT NULL,
    WasteCollected INT           NOT NULL,
    RecycledWaste  INT           NOT NULL
);

-- Sample data
INSERT INTO dbo.WasteData (City, [Year], WasteCollected, RecycledWaste) VALUES
('Amman', 2023, 12000, 3200),
('Amman', 2024, 13500, 4100),
('Zarqa', 2023,  6800, 1500),
('Zarqa', 2024,  7200, 1700),
('Irbid', 2023,  5400, 1100),
('Irbid', 2024,  5900, 1300);
